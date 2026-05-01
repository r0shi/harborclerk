"""Tests for the one-shot 0017 storage-key rename pass."""

import io
import uuid
from unittest.mock import patch

import pytest

from harbor_clerk.maintenance.rename_originals import rename_all
from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.storage import FilesystemBackend


@pytest.fixture
def fs_backend(tmp_path):
    """FilesystemBackend pointed at a temporary directory."""
    with patch("harbor_clerk.storage.get_settings") as mock_settings:
        mock_settings.return_value.storage_path = str(tmp_path)
        backend = FilesystemBackend()
    backend.ensure_bucket("originals")
    return backend


@pytest.fixture
def sync_session(_engine):
    """Synchronous SQLAlchemy session sharing the test engine (psycopg2)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = str(_engine.url).replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url, echo=False)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_doc(session, doc_id: uuid.UUID, filename: str) -> Document:
    """Create and persist a Document with the new-style key."""
    doc = Document(
        doc_id=doc_id,
        title="Test",
        canonical_filename=filename,
        status="active",
        sha256=b"x" * 32,
        pipeline_status=PipelineStatus.ready,
        original_bucket="originals",
        original_object_key=f"originals/docs/{doc_id}/{filename}",
    )
    session.add(doc)
    session.commit()
    return doc


def _put(backend: FilesystemBackend, key: str, data: bytes = b"data") -> None:
    backend.put_object("originals", key, io.BytesIO(data), len(data))


def test_rename_one_renamed_one_orphan(sync_session, fs_backend):
    """One Document with a versions-prefixed source object → renamed.
    A second versions-prefixed object with no Document → orphan, deleted.
    """
    doc_id = uuid.uuid4()
    doc = _make_doc(sync_session, doc_id, "foo.pdf")

    old_key = f"originals/versions/{uuid.uuid4()}/foo.pdf"
    orphan_key = f"originals/versions/{uuid.uuid4()}/orphan.pdf"
    _put(fs_backend, old_key)
    _put(fs_backend, orphan_key)

    with (
        patch("harbor_clerk.maintenance.rename_originals.get_storage", return_value=fs_backend),
        patch("harbor_clerk.maintenance.rename_originals.get_settings") as mock_settings,
    ):
        mock_settings.return_value.minio_bucket = "originals"
        renamed, orphans = rename_all(sync_session)

    assert renamed == 1
    assert orphans == 1

    # New key exists; old key is gone
    new_key = doc.original_object_key
    assert fs_backend.get_object("originals", new_key).read() == b"data"
    with pytest.raises(FileNotFoundError):
        fs_backend.get_object("originals", old_key)
    with pytest.raises(FileNotFoundError):
        fs_backend.get_object("originals", orphan_key)


def test_rename_idempotent(sync_session, fs_backend):
    """A second invocation when nothing is under originals/versions/ is a no-op."""
    doc_id = uuid.uuid4()
    _make_doc(sync_session, doc_id, "bar.pdf")

    # No objects under the old prefix — second run should skip cleanly
    with (
        patch("harbor_clerk.maintenance.rename_originals.get_storage", return_value=fs_backend),
        patch("harbor_clerk.maintenance.rename_originals.get_settings") as mock_settings,
    ):
        mock_settings.return_value.minio_bucket = "originals"
        renamed, orphans = rename_all(sync_session)

    assert renamed == 0
    assert orphans == 0


def test_rename_multiple_docs_distinct_filenames(sync_session, fs_backend):
    """Two documents with different filenames → both renamed correctly."""
    doc_id_a = uuid.uuid4()
    doc_id_b = uuid.uuid4()
    _make_doc(sync_session, doc_id_a, "alpha.pdf")
    _make_doc(sync_session, doc_id_b, "beta.pdf")

    old_a = f"originals/versions/{uuid.uuid4()}/alpha.pdf"
    old_b = f"originals/versions/{uuid.uuid4()}/beta.pdf"
    _put(fs_backend, old_a, b"alpha-data")
    _put(fs_backend, old_b, b"beta-data")

    with (
        patch("harbor_clerk.maintenance.rename_originals.get_storage", return_value=fs_backend),
        patch("harbor_clerk.maintenance.rename_originals.get_settings") as mock_settings,
    ):
        mock_settings.return_value.minio_bucket = "originals"
        renamed, orphans = rename_all(sync_session)

    assert renamed == 2
    assert orphans == 0
    assert fs_backend.get_object("originals", f"originals/docs/{doc_id_a}/alpha.pdf").read() == b"alpha-data"
    assert fs_backend.get_object("originals", f"originals/docs/{doc_id_b}/beta.pdf").read() == b"beta-data"
