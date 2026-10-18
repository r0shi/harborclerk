"""#619: a watched-folder document has a chunk ceiling.

Uploads are bounded by max_file_size_mb; a watched folder had no bound, and
the CUAD dataset's own 38 MB JSON dump became 57,180 chunks that held the
embedder for the whole hour-long stage timeout. Past the ceiling the document
fails at the chunk stage, with a message that says why and what to do.
"""

import hashlib

import pytest
from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

# ~12 chunks at the default 1,000-character target with 150 of overlap.
PAGE_TEXT = " ".join(f"Sentence number {i} of a long watched document." for i in range(220))


async def _make_doc(db_session, name: str, *, upload: bool = False):
    doc = Document(
        title=name,
        canonical_filename=f"{name}.txt",
        status="active",
        sha256=hashlib.sha256(name.encode()).digest(),
        source_path=f"/watched/{name}.txt",
        original_object_key=f"originals/x/{name}.txt" if upload else None,
        pipeline_status=PipelineStatus.chunking,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=PAGE_TEXT))
    # A stale chunk from the file's previous version.
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="previous version", language="english"))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.running))
    await db_session.commit()
    return doc.doc_id


def _outcome(doc_id):
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.chunk)
        ).scalar_one()
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        chunks = session.execute(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)).scalar_one()
        return job.status, job.error, doc.pipeline_status, doc.error, chunks
    finally:
        session.close()


def _run_chunk_job(doc_id, monkeypatch):
    from harbor_clerk.worker import entry as entry_mod

    # Only the chunk stage is under test; what it would enqueue next is not.
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    entry_mod.execute_job(doc_id, JobStage.chunk, 0)


@pytest.mark.asyncio
async def test_watched_document_over_the_ceiling_fails_with_a_clear_reason(db_session, monkeypatch):
    monkeypatch.setenv("WATCHED_MAX_CHUNKS_PER_DOCUMENT", "5")
    doc_id = await _make_doc(db_session, "dump")

    _run_chunk_job(doc_id, monkeypatch)

    job_status, job_error, pipeline_status, doc_error, chunks = _outcome(doc_id)
    assert job_status == JobStatus.error
    assert pipeline_status == PipelineStatus.error
    assert "exceeds the watched-folder limit of 5" in doc_error, doc_error
    assert "WATCHED_MAX_CHUNKS_PER_DOCUMENT" in doc_error
    assert job_error == doc_error
    assert chunks == 0, "no chunks may reach the embedder, and the previous version's must go"


@pytest.mark.asyncio
async def test_watched_document_under_the_ceiling_is_chunked(db_session, monkeypatch):
    monkeypatch.setenv("WATCHED_MAX_CHUNKS_PER_DOCUMENT", "100")
    doc_id = await _make_doc(db_session, "normal")

    _run_chunk_job(doc_id, monkeypatch)

    job_status, _job_error, _ps, _de, chunks = _outcome(doc_id)
    assert job_status == JobStatus.done
    assert 5 < chunks <= 100


@pytest.mark.asyncio
async def test_the_ceiling_applies_to_watched_folders_only(db_session, monkeypatch):
    """An upload is an explicit act, already bounded by max_file_size_mb."""
    monkeypatch.setenv("WATCHED_MAX_CHUNKS_PER_DOCUMENT", "5")
    doc_id = await _make_doc(db_session, "uploaded", upload=True)

    _run_chunk_job(doc_id, monkeypatch)

    job_status, job_error, _ps, _de, chunks = _outcome(doc_id)
    assert job_status == JobStatus.done, job_error
    assert chunks > 5


@pytest.mark.asyncio
async def test_a_ceiling_of_zero_disables_it(db_session, monkeypatch):
    monkeypatch.setenv("WATCHED_MAX_CHUNKS_PER_DOCUMENT", "0")
    doc_id = await _make_doc(db_session, "unbounded")

    _run_chunk_job(doc_id, monkeypatch)

    job_status, job_error, _ps, _de, chunks = _outcome(doc_id)
    assert job_status == JobStatus.done, job_error
    assert chunks > 5


def test_the_default_ceiling_lets_a_large_book_through():
    """20,000 chunks: a 5,000-page book at 3,000 characters a page still passes."""
    from harbor_clerk.config import Settings

    settings = Settings()
    new_chars_per_chunk = settings.chunk_target_size - settings.chunk_overlap
    assert settings.watched_max_chunks_per_document * new_chars_per_chunk >= 5_000 * 3_000
