"""A superseded run writes nothing (#862 review 9).

While a stage works, the user cancels and clicks Retry, and a newer claim of
the same row runs and finishes. The old run then reaches its race check. That
check compared only `pipeline_seq`, which none of this changes, so the old run
went on to write: chunk deleted the new claim's embedded chunks and wrote
unembedded ones (a ready document with no vectors), and extract rewrote the
pages. Every stage's race check before its writes is now `stage_run_is_current`,
which also asks whether the row is still this run's claim.

Each test hooks the stage's own work function to make the race happen there.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

NEW_RUN_CHUNKS = 6


_STORED_KEYS: list[str] = []


def _store_original(name: str, body: bytes, mime: str) -> str:
    """Store ``body`` as a document's original (read through watched_source, #851); removed after the test."""
    import io
    import uuid

    from harbor_clerk.storage import get_storage

    key = f"originals/docs/{uuid.uuid4()}/{name}"
    get_storage().put_object("originals", key, io.BytesIO(body), len(body), mime)
    _STORED_KEYS.append(key)
    return key


@pytest.fixture(autouse=True)
def _remove_stored_originals():
    yield
    from harbor_clerk.storage import get_storage

    while _STORED_KEYS:
        get_storage().remove_object("originals", _STORED_KEYS.pop())


async def _make_doc(db_session, stage: JobStage, tmp_path, *, mime="text/plain", page_text=None, chunks=0):
    """A document whose `stage` row is running under a claim made 30 s ago; returns (doc_id, claim)."""
    suffix = {"text/plain": "txt", "application/pdf": "pdf", "image/png": "png"}[mime]
    name = f"{stage.value}.{suffix}"
    key = _store_original(name, b"The old run's text about leases. " * 40, mime)
    claim = datetime.now(UTC) - timedelta(seconds=30)
    doc = Document(
        title=f"superseded-{stage.value}",
        canonical_filename=name,
        status="active",
        sha256=hashlib.sha256(f"superseded-{stage.value}-{mime}".encode()).digest(),
        original_bucket="originals",
        original_object_key=key,
        mime_type=mime,
        needs_ocr=stage == JobStage.ocr,
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add(doc)
    await db_session.flush()
    if page_text is not None:
        db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=page_text))
    for n in range(chunks):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=n, chunk_text=f"passage {n}", language="english"))
    db_session.add(
        IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.running, started_at=claim, heartbeat_at=claim)
    )
    await db_session.commit()
    return doc.doc_id, claim


def _a_newer_claim_finishes(doc_id, stage: JobStage, *, chunks=False, page_text=None) -> None:
    """Meanwhile: cancel, Retry (which clears started_at) and a new claim that ran and finished."""
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == stage)
        ).scalar_one()
        job.status, job.started_at = JobStatus.done, datetime.now(UTC)
        if chunks:
            for c in session.execute(select(Chunk).where(Chunk.doc_id == doc_id)).scalars():
                session.delete(c)
            for i in range(NEW_RUN_CHUNKS):
                session.add(
                    Chunk(
                        doc_id=doc_id,
                        chunk_num=i,
                        chunk_text=f"new run chunk {i}",
                        language="english",
                        embedding=[0.01] * 768,
                    )
                )
        if page_text is not None:
            for page in session.execute(select(DocumentPage).where(DocumentPage.doc_id == doc_id)).scalars():
                page.page_text = page_text
        session.commit()
    finally:
        session.close()


def _run(doc_id, stage: JobStage, claim, monkeypatch) -> None:
    from harbor_clerk.worker import entry as entry_mod

    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    entry_mod.execute_job(doc_id, stage, 0, claim)


def _job(doc_id, stage: JobStage):
    session = get_sync_session()
    try:
        return session.execute(
            select(IngestionJob.status).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == stage)
        ).scalar_one()
    finally:
        session.close()


def _page_texts(doc_id) -> list[str]:
    session = get_sync_session()
    try:
        return list(session.execute(select(DocumentPage.page_text).where(DocumentPage.doc_id == doc_id)).scalars())
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_superseded_chunk_run_leaves_the_new_claims_chunks(db_session, monkeypatch, tmp_path):
    """The reviewer's probe: the new claim chunked and embedded the document."""
    from harbor_clerk.worker.stages import chunk as chunk_stage

    doc_id, claim = await _make_doc(db_session, JobStage.chunk, tmp_path, page_text="A sentence about leases. " * 200)
    real_split = chunk_stage._split_text

    def _split(*args, **kwargs):
        _a_newer_claim_finishes(doc_id, JobStage.chunk, chunks=True)
        return real_split(*args, **kwargs)

    monkeypatch.setattr(chunk_stage, "_split_text", _split)
    _run(doc_id, JobStage.chunk, claim, monkeypatch)

    session = get_sync_session()
    try:
        total, embedded = session.execute(
            select(func.count(Chunk.chunk_id), func.count(Chunk.embedding)).where(Chunk.doc_id == doc_id)
        ).one()
    finally:
        session.close()
    assert (total, embedded) == (NEW_RUN_CHUNKS, NEW_RUN_CHUNKS), f"the old run rewrote the chunks: {total}, {embedded}"
    assert _job(doc_id, JobStage.chunk) == JobStatus.done


@pytest.mark.asyncio
async def test_a_superseded_extract_run_leaves_the_new_claims_pages(db_session, monkeypatch, tmp_path):
    from harbor_clerk.worker.stages import extract as extract_stage

    doc_id, claim = await _make_doc(db_session, JobStage.extract, tmp_path, page_text="")
    real_sniff = extract_stage.sniff_mime

    def _sniff(data):
        _a_newer_claim_finishes(doc_id, JobStage.extract, page_text="the new claim's pages")
        return real_sniff(data)

    monkeypatch.setattr(extract_stage, "sniff_mime", _sniff)
    _run(doc_id, JobStage.extract, claim, monkeypatch)

    assert _page_texts(doc_id) == ["the new claim's pages"], "the old run rewrote the pages"


@pytest.mark.asyncio
async def test_a_superseded_entities_run_writes_no_entities(db_session, monkeypatch, tmp_path):
    from harbor_clerk.models.entity import Entity
    from harbor_clerk.worker.ner import EntitySpan
    from harbor_clerk.worker.stages import entities as entities_stage

    doc_id, claim = await _make_doc(db_session, JobStage.entities, tmp_path, chunks=2)

    def _ner(batch):
        _a_newer_claim_finishes(doc_id, JobStage.entities)
        return [[EntitySpan(text="Port", type="ORG", start_char=0, end_char=4)] for _ in batch]

    monkeypatch.setattr(entities_stage, "extract_entities_batch", _ner)
    _run(doc_id, JobStage.entities, claim, monkeypatch)

    session = get_sync_session()
    try:
        assert not session.execute(select(Entity).where(Entity.doc_id == doc_id)).scalars().all(), (
            "the old run wrote entities"
        )
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_superseded_summarize_run_writes_no_summary(db_session, monkeypatch, tmp_path):
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc_id, claim = await _make_doc(db_session, JobStage.summarize, tmp_path, chunks=3)

    def _generate_summary(chunks, *, yield_check=None, progress_callback=None):
        yield_check()
        _a_newer_claim_finishes(doc_id, JobStage.summarize)  # after the last yield check
        return "A summary from the superseded run", "model"

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *a, **k: None)
    monkeypatch.setattr(summarize_stage, "generate_summary", _generate_summary)
    monkeypatch.setattr(summarize_stage, "classify_doc_type", lambda *a, **k: "contract")
    _run(doc_id, JobStage.summarize, claim, monkeypatch)

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        assert doc.summary is None and doc.doc_type is None, "the old run wrote its summary"
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["application/pdf", "image/png"], ids=["pdf", "image"])
async def test_a_superseded_ocr_run_writes_no_page(db_session, monkeypatch, tmp_path, mime):
    from PIL import Image

    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id, claim = await _make_doc(db_session, JobStage.ocr, tmp_path, mime=mime, page_text="")
    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8))])

    def _ocr(image_bytes, lang):
        _a_newer_claim_finishes(doc_id, JobStage.ocr, page_text="the new claim's OCR")
        return "the old run's OCR", 90.0

    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", _ocr)
    _run(doc_id, JobStage.ocr, claim, monkeypatch)

    assert _page_texts(doc_id) == ["the new claim's OCR"], "the old run wrote its OCR"
