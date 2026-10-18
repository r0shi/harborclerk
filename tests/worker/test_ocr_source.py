"""The OCR stage reads a document's bytes as the extract stage does: a watched
document from its file, even when it also carries an older stored original (#859)."""

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


@pytest.mark.asyncio
async def test_ocr_reads_a_watched_doc_from_disk_despite_a_stale_stored_original(
    db_session, tmp_path: Path, monkeypatch
):
    from harbor_clerk.worker.stages import ocr

    seen: list[bytes] = []

    def fake_ocr(data: bytes, lang: str):
        seen.append(data)
        return data.decode(), 99.0

    monkeypatch.setattr(ocr, "_ocr_image_bytes", fake_ocr)

    src = tmp_path / "scan.png"
    src.write_bytes(b"edited on disk")
    key = f"originals/docs/{uuid.uuid4()}/scan.png"
    get_storage().put_object("originals", key, io.BytesIO(b"stale stored"), len(b"stale stored"), "image/png")

    doc = Document(
        title="scan",
        canonical_filename="scan.png",
        status="active",
        sha256=hashlib.sha256(b"edited on disk").digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="image/png",
        source_path=str(src),
        original_bucket="originals",
        original_object_key=key,
        needs_ocr=True,
    )
    db_session.add(doc)
    folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
    db_session.add(folder)
    await db_session.flush()
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

    try:
        ocr.run_ocr(doc_id)
    finally:
        get_storage().remove_object("originals", key)

    assert seen == [b"edited on disk"]
    sync_session = get_sync_session()
    try:
        page = sync_session.execute(select(DocumentPage).where(DocumentPage.doc_id == doc_id)).scalar_one()
    finally:
        sync_session.close()
    assert page.page_text == "edited on disk"
