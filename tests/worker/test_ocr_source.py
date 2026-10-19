"""The OCR stage reads a document's bytes as the extract stage does: a watched
document from its file, even when it also carries an older stored original (#859),
and from storage when that file is gone."""

import hashlib
import io
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.storage import get_storage


async def _ocr_watched_scan(
    db_session,
    tmp_path: Path,
    monkeypatch,
    *,
    delete_file: bool,
    stored_name: str = "scan.png",
    mime_type: str = "image/png",
    raises: type[BaseException] | None = None,
    watched: bool = True,
    stored: bool = True,
) -> tuple[list[bytes], str]:
    """OCR an image whose file holds b"edited on disk", mapped to a watched file
    unless ``watched`` is false, with a stored original holding b"stale stored"
    unless ``stored`` is false. Returns (bytes OCR saw, page text)."""
    from harbor_clerk.worker.stages import ocr

    seen: list[bytes] = []

    def fake_ocr(data: bytes, lang: str):
        seen.append(data)
        return data.decode(), 99.0

    monkeypatch.setattr(ocr, "_ocr_image_bytes", fake_ocr)

    src = tmp_path / "scan.png"
    src.write_bytes(b"edited on disk")
    key = f"originals/docs/{uuid.uuid4()}/{stored_name}" if stored else None
    if key:
        get_storage().put_object("originals", key, io.BytesIO(b"stale stored"), len(b"stale stored"), "image/png")

    doc = Document(
        title="scan",
        canonical_filename="scan.png",
        status="active",
        sha256=hashlib.sha256(b"edited on disk").digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type=mime_type,
        source_path=str(src),
        original_bucket="originals" if key else None,
        original_object_key=key,
        needs_ocr=True,
    )
    db_session.add(doc)
    folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
    db_session.add(folder)
    await db_session.flush()
    if watched:
        db_session.add(
            WatchedFile(
                folder_id=folder.folder_id,
                relative_path=src.name,
                bookmark_data=b"",
                sha256=doc.sha256,
                doc_id=doc.doc_id,
                status=WatchedFileStatus.active,
            )
        )
    db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=""))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.ocr, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id
    if delete_file:
        src.unlink()

    try:
        if raises is None:
            ocr.run_ocr(doc_id)
        else:
            with pytest.raises(raises):
                ocr.run_ocr(doc_id)
    finally:
        if key:
            get_storage().remove_object("originals", key)

    sync_session = get_sync_session()
    try:
        page = sync_session.execute(select(DocumentPage).where(DocumentPage.doc_id == doc_id)).scalar_one()
        return seen, page.page_text
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_ocr_reads_a_watched_doc_from_disk_despite_a_stale_stored_original(
    db_session, tmp_path: Path, monkeypatch
):
    seen, text = await _ocr_watched_scan(db_session, tmp_path, monkeypatch, delete_file=False)
    assert seen == [b"edited on disk"]
    assert text == "edited on disk"


@pytest.mark.asyncio
async def test_ocr_fails_when_the_watched_file_is_missing(db_session, tmp_path: Path, monkeypatch):
    """A missing watched file fails the stage; its stored original (maybe an older upload) is not OCRed."""
    from harbor_clerk.watched_source import WatchedFileMissing

    seen, text = await _ocr_watched_scan(db_session, tmp_path, monkeypatch, delete_file=True, raises=WatchedFileMissing)
    assert seen == []
    assert text == ""


@pytest.mark.asyncio
async def test_ocr_never_reads_an_unverified_source_path(db_session, tmp_path: Path, monkeypatch):
    """Not read in place and no stored original: the stage fails, without reading source_path."""
    from harbor_clerk.watched_source import NoReadableSource

    seen, text = await _ocr_watched_scan(
        db_session, tmp_path, monkeypatch, delete_file=False, watched=False, stored=False, raises=NoReadableSource
    )
    assert seen == []
    assert text == ""


@pytest.mark.asyncio
async def test_ocr_does_not_treat_a_watched_file_as_pdf_because_of_a_stale_original_name(
    db_session, tmp_path: Path, monkeypatch
):
    """The PDF check reads the file's name (scan.png), not the stale stored original's (scan.pdf)."""
    from harbor_clerk.worker.stages import ocr

    pdf_calls: list[bytes] = []

    def fake_pdf_to_images(data: bytes):
        pdf_calls.append(data)
        return []

    monkeypatch.setattr(ocr, "_pdf_to_images", fake_pdf_to_images)
    await _ocr_watched_scan(
        db_session,
        tmp_path,
        monkeypatch,
        delete_file=False,
        stored_name="scan.pdf",
        mime_type="application/octet-stream",
    )
    assert pdf_calls == []


@pytest.mark.asyncio
async def test_ocr_fails_when_the_watched_file_lookup_fails(db_session, tmp_path: Path, monkeypatch):
    """A failed watched-file lookup fails the stage; the stored original is not OCRed."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    import harbor_clerk.watched_source as watched_source

    monkeypatch.setattr(watched_source, "watched_paths_query", lambda doc_id: text("SELECT * FROM no_such_table_603"))
    seen, text_after = await _ocr_watched_scan(
        db_session, tmp_path, monkeypatch, delete_file=False, raises=SQLAlchemyError
    )
    assert seen == []
    assert text_after == ""
