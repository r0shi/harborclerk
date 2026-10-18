"""scripts/reextract_metadata.py reads a document's bytes as the extract stage
does: from storage when the original is stored, in place otherwise; and it reads
a sidecar only for a document read in place from a watched folder."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.storage import get_storage


def _md(vendor: str) -> bytes:
    return f"---\nvendor: {vendor}\n---\nBody.\n".encode()


@pytest.fixture(autouse=True)
def _no_tika(monkeypatch):
    """Keep the run hermetic: the Tika extractor is not under test."""
    from harbor_clerk.ingest import metadata_extractors
    from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor
    from harbor_clerk.ingest.metadata_extractors.sidecar import SidecarExtractor

    monkeypatch.setattr(metadata_extractors, "EXTRACTORS", [FrontmatterExtractor(), SidecarExtractor()])


@pytest.fixture(autouse=True)
def _script_session(_engine, monkeypatch):
    """Run the script on the test engine (NullPool), not the module-level pool bound to another loop."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import scripts.reextract_metadata as script

    monkeypatch.setattr(
        script, "async_session_factory", async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    )


async def test_reextract_reads_stored_original_and_no_sidecar_for_an_upload(db_session, tmp_path: Path):
    """An upload is read from storage, not from the file its source_path names."""
    import scripts.reextract_metadata as script

    stored = _md("Stored")
    labelled = tmp_path / "note.md"
    labelled.write_bytes(_md("Labelled"))
    (tmp_path / "note.json").write_text(json.dumps({"vendor": "Beside"}))
    key = f"originals/docs/{uuid.uuid4()}/note.md"
    get_storage().put_object("originals", key, io.BytesIO(stored), len(stored), "text/markdown")

    doc = Document(
        title="note",
        canonical_filename="note.md",
        status="active",
        sha256=hashlib.sha256(stored).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/markdown",
        source_path=str(labelled),
        original_bucket="originals",
        original_object_key=key,
    )
    db_session.add(doc)
    await db_session.commit()
    doc_id = doc.doc_id

    try:
        await script.reextract_all()
    finally:
        get_storage().remove_object("originals", key)

    db_session.expire_all()
    refreshed = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert refreshed.doc_metadata["frontmatter"] == {"vendor": "Stored"}
    assert "sidecar" not in refreshed.doc_metadata


async def test_reextract_reads_a_watched_doc_in_place_with_its_sidecar(db_session, tmp_path: Path):
    import scripts.reextract_metadata as script

    source = tmp_path / "watched.md"
    source.write_bytes(_md("OnDisk"))
    (tmp_path / "watched.json").write_text(json.dumps({"vendor": "Beside"}))

    doc = Document(
        title="watched",
        canonical_filename="watched.md",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/markdown",
        source_path=str(source),
    )
    db_session.add(doc)
    folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=source.name,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.commit()
    doc_id = doc.doc_id

    await script.reextract_all()

    db_session.expire_all()
    refreshed = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert refreshed.doc_metadata["frontmatter"] == {"vendor": "OnDisk"}
    assert refreshed.doc_metadata["sidecar"] == {"vendor": "Beside"}


async def test_reextract_skips_an_unverified_source_path(db_session, tmp_path: Path):
    """Not read in place and no stored original: skipped, without reading the file at source_path."""
    import scripts.reextract_metadata as script

    source = tmp_path / "label.md"
    source.write_bytes(_md("AtTheLabel"))
    doc = Document(
        title="label",
        canonical_filename="label.md",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/markdown",
        source_path=str(source),
        doc_metadata={},
    )
    db_session.add(doc)
    await db_session.commit()
    doc_id = doc.doc_id

    processed, updated, skipped = await script.reextract_all()

    assert (processed, updated, skipped) == (1, 0, 1)
    db_session.expire_all()
    refreshed = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert refreshed.doc_metadata == {}


async def test_reextract_skips_a_watched_doc_whose_file_is_missing(db_session, tmp_path: Path):
    """A missing watched file is skipped, never replaced by the stored original."""
    import scripts.reextract_metadata as script

    stored = _md("Stored")
    source = tmp_path / "gone.md"
    key = f"originals/docs/{uuid.uuid4()}/gone.md"
    get_storage().put_object("originals", key, io.BytesIO(stored), len(stored), "text/markdown")
    doc = Document(
        title="gone",
        canonical_filename="gone.md",
        status="active",
        sha256=hashlib.sha256(stored).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/markdown",
        source_path=str(source),
        original_bucket="originals",
        original_object_key=key,
        doc_metadata={},
    )
    db_session.add(doc)
    folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=source.name,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.commit()
    doc_id = doc.doc_id

    try:
        processed, updated, skipped = await script.reextract_all()
    finally:
        get_storage().remove_object("originals", key)

    assert (processed, updated, skipped) == (1, 0, 1)
    db_session.expire_all()
    refreshed = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert refreshed.doc_metadata == {}


async def test_reextract_skips_a_removed_watched_file_with_a_stored_original(db_session, tmp_path: Path):
    """A watched document whose watched file is removed is skipped, never read from its stored original."""
    import scripts.reextract_metadata as script

    stored = _md("Stored")
    source = tmp_path / "removed.md"
    source.write_bytes(_md("OnDisk"))
    key = f"originals/docs/{uuid.uuid4()}/removed.md"
    get_storage().put_object("originals", key, io.BytesIO(stored), len(stored), "text/markdown")
    doc = Document(
        title="removed",
        canonical_filename="removed.md",
        status="active",
        sha256=hashlib.sha256(stored).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/markdown",
        source_path=str(source),
        original_bucket="originals",
        original_object_key=key,
        doc_metadata={},
    )
    db_session.add(doc)
    folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=source.name,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.removed,
        )
    )
    await db_session.commit()
    doc_id = doc.doc_id

    try:
        processed, updated, skipped = await script.reextract_all()
    finally:
        get_storage().remove_object("originals", key)

    assert (processed, updated, skipped) == (1, 0, 1)
    db_session.expire_all()
    refreshed = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert refreshed.doc_metadata == {}


@pytest.mark.parametrize("breakage", ["not-a-directory", "permission"])
async def test_reextract_skips_an_unreadable_watched_file_and_carries_on(db_session, tmp_path: Path, breakage):
    """Its folder replaced by a file, or no permission: the document is skipped and
    the run goes on to the next one."""
    import os
    import shutil

    import scripts.reextract_metadata as script
    from tests.conftest import watch_document

    if breakage == "permission" and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    folder = tmp_path / "folder"
    folder.mkdir()
    broken = folder / "broken.md"
    broken.write_bytes(_md("Broken"))
    fine_dir = tmp_path / "fine"
    fine_dir.mkdir()
    fine = fine_dir / "fine.md"
    fine.write_bytes(_md("Fine"))
    ids = []
    for source in (broken, fine):
        doc = Document(
            title=source.stem,
            canonical_filename=source.name,
            status="active",
            sha256=hashlib.sha256(source.read_bytes()).digest(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/markdown",
            source_path=str(source),
            doc_metadata={},
        )
        db_session.add(doc)
        await db_session.flush()
        await watch_document(db_session, doc, source)
        ids.append(doc.doc_id)
    await db_session.commit()
    if breakage == "not-a-directory":
        shutil.rmtree(folder)
        folder.write_text("a file where the folder was")
    else:
        broken.chmod(0)

    try:
        processed, updated, skipped = await script.reextract_all()
    finally:
        if breakage == "permission":
            broken.chmod(0o644)

    assert (processed, updated, skipped) == (2, 1, 1)
    db_session.expire_all()
    fine_doc = (await db_session.execute(select(Document).where(Document.doc_id == ids[1]))).scalar_one()
    assert fine_doc.doc_metadata["frontmatter"] == {"vendor": "Fine"}
