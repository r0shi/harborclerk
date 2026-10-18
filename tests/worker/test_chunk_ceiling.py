"""#619: every document has a chunk ceiling, whatever its source.

A watched folder had no bound, and the CUAD dataset's own 38 MB JSON dump became
57,180 chunks that held the embedder for the whole hour-long stage timeout.
max_file_size_mb does not bound uploads in chunks (200 MB of text is ~235k) or
IMAP attachments at all. Past the ceiling the document fails at the chunk
stage, with a message that names the cause and the setting.
"""

import hashlib

import pytest
from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

# ~12 chunks at the default 1,000-character target with 150 of overlap.
PAGE_TEXT = " ".join(f"Sentence number {i} of a long document." for i in range(220))

# source_path, original_object_key per kind of document.
SOURCES = {
    "watched": ("/watched/{name}.txt", None),
    "upload": ("{name}.txt", "originals/x/{name}.txt"),
    "imap": (None, "mail/x/{name}.eml"),
}


async def _make_doc(db_session, name: str, *, source: str = "watched", previous_index: bool = True):
    source_path, object_key = SOURCES[source]
    doc = Document(
        title=name,
        canonical_filename=f"{name}.txt",
        status="active",
        sha256=hashlib.sha256(name.encode()).digest(),
        source_path=source_path.format(name=name) if source_path else None,
        original_object_key=object_key.format(name=name) if object_key else None,
        pipeline_status=PipelineStatus.chunking,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=PAGE_TEXT))
    if previous_index:
        # A stale chunk from the file's previous version.
        db_session.add(
            Chunk(
                doc_id=doc.doc_id,
                chunk_num=0,
                chunk_text="previous version",
                language="english",
                embedding=[0.01] * 768,
            )
        )
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
@pytest.mark.parametrize("source", list(SOURCES))
async def test_a_document_over_the_ceiling_fails_with_a_clear_reason(db_session, monkeypatch, source):
    """The recorded error names the cause and the setting, so an operator whose
    existing large document goes to error on reprocess can see why."""
    monkeypatch.setenv("MAX_CHUNKS_PER_DOCUMENT", "5")
    doc_id = await _make_doc(db_session, f"dump-{source}", source=source)

    _run_chunk_job(doc_id, monkeypatch)

    job_status, job_error, pipeline_status, doc_error, chunks = _outcome(doc_id)
    assert job_status == JobStatus.error
    assert pipeline_status == PipelineStatus.error
    assert "Document too large to index" in doc_error, doc_error
    assert "over the per-document limit of 5" in doc_error
    assert "MAX_CHUNKS_PER_DOCUMENT" in doc_error
    assert "max_chunks_per_document in the Mac app's config.json" in doc_error, "the Mac app's knob must be named too"
    assert job_error == doc_error
    assert "previous version's index is still searchable until the document fits" in doc_error, doc_error
    # The existing index is left alone: an over-limit document reprocessed after
    # an upgrade keeps being found while the error says why it was not re-indexed.
    assert chunks == 1, f"the existing chunks were touched: {chunks}"
    session = get_sync_session()
    try:
        kept = session.execute(select(Chunk).where(Chunk.doc_id == doc_id)).scalar_one()
        assert kept.chunk_text == "previous version" and kept.embedding is not None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_new_document_over_the_ceiling_claims_no_previous_index(db_session, monkeypatch):
    monkeypatch.setenv("MAX_CHUNKS_PER_DOCUMENT", "5")
    doc_id = await _make_doc(db_session, "new-dump", previous_index=False)

    _run_chunk_job(doc_id, monkeypatch)

    job_status, _job_error, _pipeline_status, doc_error, chunks = _outcome(doc_id)
    assert job_status == JobStatus.error and chunks == 0
    assert "Document too large to index" in doc_error
    assert "previous version" not in doc_error, doc_error


@pytest.mark.asyncio
async def test_a_document_under_the_ceiling_is_chunked(db_session, monkeypatch):
    monkeypatch.setenv("MAX_CHUNKS_PER_DOCUMENT", "100")
    doc_id = await _make_doc(db_session, "normal")

    _run_chunk_job(doc_id, monkeypatch)

    job_status, _job_error, _ps, _de, chunks = _outcome(doc_id)
    assert job_status == JobStatus.done
    assert 5 < chunks <= 100


@pytest.mark.asyncio
async def test_a_ceiling_of_zero_disables_it(db_session, monkeypatch):
    monkeypatch.setenv("MAX_CHUNKS_PER_DOCUMENT", "0")
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
    assert settings.max_chunks_per_document * new_chars_per_chunk >= 5_000 * 3_000
