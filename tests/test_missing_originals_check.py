"""The API startup check for documents whose stored originals are missing (#888).

A normal upgrade (`git pull && docker compose up -d --build`) starts the
filesystem-backed stack on an empty `originals` volume. Compose always sets
STORAGE_BACKEND, so get_storage()'s unset-variable warning never fires; this
ERROR is what points the operator at the MinIO migration.
"""

import io
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import harbor_clerk.storage as storage_mod
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.storage import FilesystemBackend, log_missing_originals

BUCKET = "originals"


@pytest.fixture
def fs(tmp_path, monkeypatch):
    with patch("harbor_clerk.storage.get_settings") as settings:
        settings.return_value.storage_path = str(tmp_path)
        backend = FilesystemBackend()
    monkeypatch.setattr(storage_mod, "get_storage", lambda: backend)
    return backend


async def _add_document(session, *, key: str | None, status: str = "active", n: int = 0) -> None:
    session.add(
        Document(
            title=f"Missing originals check {n}",
            canonical_filename=f"doc{n}.pdf",
            status=status,
            sha256=bytes([n + 1] * 32),
            pipeline_status=PipelineStatus.ready,
            original_bucket=BUCKET if key else None,
            original_object_key=key,
        )
    )
    await session.commit()


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.asyncio
async def test_stored_originals_and_an_empty_bucket_log_an_error(db_session, fs, tmp_path, caplog):
    await _add_document(db_session, key="originals/docs/1/a.pdf")
    fs.ensure_bucket(BUCKET)  # what startup does just before the check
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is True
    [message] = _errors(caplog)
    assert "docs/upgrade-runbook.md" in message and "MinIO" in message
    assert str((tmp_path / BUCKET).resolve()) in message


@pytest.mark.asyncio
async def test_a_missing_bucket_directory_counts_as_empty(db_session, fs, caplog):
    await _add_document(db_session, key="originals/2/msg.eml")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is True
    assert len(_errors(caplog)) == 1


@pytest.mark.asyncio
async def test_a_populated_bucket_is_quiet(db_session, fs, caplog):
    await _add_document(db_session, key="originals/docs/1/a.pdf")
    fs.put_object(BUCKET, "originals/docs/1/a.pdf", io.BytesIO(b"a"), 1)
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is False
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_no_stored_originals_is_quiet(db_session, fs, caplog):
    """Watched-folder documents have no stored original; deleted ones don't count."""
    await _add_document(db_session, key=None, n=1)
    await _add_document(db_session, key="originals/docs/9/gone.pdf", status="deleted", n=2)
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is False
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_minio_backend_skips_the_check(monkeypatch):
    monkeypatch.setattr(storage_mod, "get_storage", lambda: MagicMock(spec=storage_mod.MinIOBackend))
    session = MagicMock(scalar=AsyncMock())
    assert await log_missing_originals(session) is False
    session.scalar.assert_not_awaited()
