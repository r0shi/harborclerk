"""#620's siblings: OCR and summarize stop on a cancel too.

A cancel sets the job row to `error` and leaves `pipeline_seq` alone, so any
long loop that checked only the seq ran on after it. OCR runs a page at a time
up to its 7200 s timeout; summarize runs one LLM call per map-reduce group.
Both now ask `stage_run_is_current` between units of work.
"""

import hashlib

import pytest
from PIL import Image
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import cancel_doc_jobs

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


async def _make_doc(db_session, stage: JobStage, tmp_path, *, pages: int = 0, chunks: int = 0):
    key = _store_original("scan.pdf", b"%PDF-1.4 stand-in; rendering is stubbed", "application/pdf")
    doc = Document(
        title="scan",
        canonical_filename="scan.pdf",
        status="active",
        sha256=hashlib.sha256(stage.value.encode()).digest(),
        original_bucket="originals",
        original_object_key=key,
        mime_type="application/pdf",
        needs_ocr=True,
        extracted_chars=123,
        pipeline_status=PipelineStatus.ocr_running,
    )
    db_session.add(doc)
    await db_session.flush()
    for n in range(1, pages + 1):
        db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=n, page_text=""))
    for n in range(chunks):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=n, chunk_text=f"passage {n}", language="english"))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.running))
    await db_session.commit()
    return doc.doc_id


def _read(doc_id, stage: JobStage):
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == stage)
        ).scalar_one()
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        page_texts = list(
            session.execute(select(DocumentPage.page_text).where(DocumentPage.doc_id == doc_id)).scalars()
        )
        session.expunge_all()
        return job, doc, page_texts
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_cancel_stops_ocr_between_pages(db_session, monkeypatch, tmp_path):
    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id = await _make_doc(db_session, JobStage.ocr, tmp_path, pages=4)
    pages_ocrd: list[int] = []

    def _ocr(image_bytes, lang):
        pages_ocrd.append(1)
        if len(pages_ocrd) == 2:
            assert cancel_doc_jobs(doc_id) == 1
        return "recognised text", 90.0

    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8)) for _ in range(4)])
    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", _ocr)

    ocr_stage.run_ocr(doc_id)

    job, doc, page_texts = _read(doc_id, JobStage.ocr)
    assert len(pages_ocrd) == 2, f"OCR ran {len(pages_ocrd)} of 4 pages; it should stop after the cancel"
    assert job.status == JobStatus.error and job.error == "Cancelled by user"
    assert all(t == "" for t in page_texts), "a cancelled run wrote OCR text"
    assert doc.extracted_chars == 123, "a cancelled run overwrote the document's character count"


@pytest.mark.asyncio
async def test_an_uncancelled_ocr_run_still_writes_every_page(db_session, monkeypatch, tmp_path):
    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id = await _make_doc(db_session, JobStage.ocr, tmp_path, pages=3)
    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8)) for _ in range(3)])
    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", lambda image_bytes, lang: ("recognised", 90.0))
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)

    ocr_stage.run_ocr(doc_id)

    job, _doc, page_texts = _read(doc_id, JobStage.ocr)
    assert job.status == JobStatus.done
    assert page_texts == ["recognised"] * 3


@pytest.mark.asyncio
async def test_a_run_stopped_mid_write_writes_no_page_and_a_rerun_appends_once(db_session, monkeypatch, tmp_path):
    """A shutdown deadline (or a crash) while OCR writes its pages, then the requeued
    rerun from page 1. With a commit per page, page 1 kept the first run's OCR and
    the rerun appended a second copy. The pages are now one transaction: the
    stopped run leaves every page as extract wrote it."""
    from sqlalchemy import event

    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id = await _make_doc(db_session, JobStage.ocr, tmp_path, pages=2)
    _set_page_text(doc_id, 1, "Extracted heading")
    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8)) for _ in range(2)])
    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", lambda image_bytes, lang: ("recognised", 90.0))
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)

    class _Deadline(Exception):
        pass

    def _stop_at_page_two(target, value, oldvalue, initiator):
        if target.page_num == 2:
            raise _Deadline("the deadline lands after page 1 is written")

    event.listen(DocumentPage.page_text, "set", _stop_at_page_two)
    try:
        with pytest.raises(_Deadline):
            ocr_stage.run_ocr(doc_id)
    finally:
        event.remove(DocumentPage.page_text, "set", _stop_at_page_two)
    assert sorted(_read(doc_id, JobStage.ocr)[2]) == ["", "Extracted heading"], "a stopped run left pages written"

    ocr_stage.run_ocr(doc_id)  # the rerun: the job row is still running, as the requeue leaves it claimable

    job, _doc, page_texts = _read(doc_id, JobStage.ocr)
    assert job.status == JobStatus.done
    assert sorted(page_texts) == sorted(["Extracted heading\n\n--- OCR ---\n\nrecognised", "recognised"]), page_texts


@pytest.mark.asyncio
async def test_empty_ocr_leaves_the_page_unchanged(db_session, monkeypatch, tmp_path):
    """Nothing recognised adds nothing: no marker, nothing indexed."""
    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id = await _make_doc(db_session, JobStage.ocr, tmp_path, pages=2)
    _set_page_text(doc_id, 1, "Bates HC-000123")
    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8)) for _ in range(2)])
    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", lambda image_bytes, lang: ("", 0.0))
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)

    ocr_stage.run_ocr(doc_id)

    _job, _doc, page_texts = _read(doc_id, JobStage.ocr)
    assert sorted(page_texts) == ["", "Bates HC-000123"], page_texts


@pytest.mark.asyncio
async def test_the_alarm_is_held_from_the_page_commit_through_mark_stage_done(db_session, monkeypatch, tmp_path):
    """A deadline between the commit and mark_stage_done would requeue a run whose
    pages are written, and the rerun would append its OCR a second time."""
    from harbor_clerk.worker import pipeline
    from harbor_clerk.worker.stages import ocr as ocr_stage

    doc_id = await _make_doc(db_session, JobStage.ocr, tmp_path, pages=1)
    monkeypatch.setattr(ocr_stage, "_pdf_to_images", lambda data: [Image.new("RGB", (8, 8))])
    monkeypatch.setattr(ocr_stage, "_ocr_image_bytes", lambda image_bytes, lang: ("recognised", 90.0))
    held: list[bool] = []
    monkeypatch.setattr(ocr_stage, "mark_stage_done", lambda *a, **kw: held.append(pipeline.alarm_is_deferred()))

    ocr_stage.run_ocr(doc_id)

    assert held == [True], "the alarm was not held between the page commit and mark_stage_done"
    assert not pipeline.alarm_is_deferred(), "the hold outlived the stage"


def _set_page_text(doc_id, page_num: int, text: str) -> None:
    session = get_sync_session()
    try:
        page = session.execute(
            select(DocumentPage).where(DocumentPage.doc_id == doc_id, DocumentPage.page_num == page_num)
        ).scalar_one()
        page.page_text = text
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_cancel_stops_summarize_between_map_calls(db_session, monkeypatch, tmp_path):
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc_id = await _make_doc(db_session, JobStage.summarize, tmp_path, chunks=3)
    llm_calls: list[str] = []
    classified: list[bool] = []

    def _generate_summary(chunks, *, yield_check=None, progress_callback=None):
        # The map-reduce loop: yield_check before each LLM sub-call.
        yield_check()
        llm_calls.append("map 1")
        assert cancel_doc_jobs(doc_id) == 1
        yield_check()
        llm_calls.append("map 2")
        return "A summary nobody asked for", "model"

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *a, **k: None)
    monkeypatch.setattr(summarize_stage, "generate_summary", _generate_summary)
    monkeypatch.setattr(summarize_stage, "classify_doc_type", lambda *a, **k: classified.append(True) or "contract")

    summarize_stage.run_summarize(doc_id)

    job, doc, _ = _read(doc_id, JobStage.summarize)
    assert llm_calls == ["map 1"], f"summarize kept calling the LLM after the cancel: {llm_calls}"
    assert not classified, "the classification LLM call ran for a cancelled document"
    assert doc.summary is None and doc.doc_type is None
    assert job.status == JobStatus.error and job.error == "Cancelled by user"


@pytest.mark.asyncio
async def test_a_cancelled_run_does_not_complete_a_re_enqueued_one(db_session, monkeypatch, tmp_path):
    """Cancel, then Resummarize (a same-generation re-enqueue) before the next yield
    check: the row is "queued" again, which no longer reads as cancelled, so the old
    run carried on and its mark_stage_done marked the new queued row done. A run
    now only counts as current, and only completes, the claim it made."""
    from datetime import UTC, datetime

    from harbor_clerk.worker import entry as entry_mod
    from harbor_clerk.worker.pipeline import enqueue_stage
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc_id = await _make_doc(db_session, JobStage.summarize, tmp_path, chunks=3)
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        job.started_at = datetime.now(UTC)  # as claim_next_job stamps it
        session.commit()
    finally:
        session.close()
    llm_calls: list[str] = []

    def _generate_summary(chunks, *, yield_check=None, progress_callback=None):
        yield_check()
        llm_calls.append("map 1")
        assert cancel_doc_jobs(doc_id) == 1
        enqueue_stage(doc_id, JobStage.summarize)  # Resummarize: same generation, row queued again
        yield_check()
        llm_calls.append("map 2")
        return "A summary from the cancelled run", "model"

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *a, **k: None)
    monkeypatch.setattr(summarize_stage, "generate_summary", _generate_summary)
    monkeypatch.setattr(summarize_stage, "classify_doc_type", lambda *a, **k: None)

    entry_mod.execute_job(doc_id, JobStage.summarize, 0)

    job, doc, _ = _read(doc_id, JobStage.summarize)
    assert llm_calls == ["map 1"], f"the cancelled run carried on into the re-enqueued one: {llm_calls}"
    assert job.status == JobStatus.queued, f"the old run completed the new claim: {job.status}"
    assert doc.summary is None


@pytest.mark.asyncio
async def test_a_cancel_stops_entities_between_ner_batches(db_session, monkeypatch, tmp_path):
    """Entities used to run NER over every chunk in one call, on the io queue, up to
    its 900 s timeout, deaf to a cancel."""
    from harbor_clerk.models.entity import Entity
    from harbor_clerk.worker.ner import EntitySpan
    from harbor_clerk.worker.stages import entities as entities_stage

    chunks = entities_stage.NER_BATCH_SIZE * 2 + 1
    doc_id = await _make_doc(db_session, JobStage.entities, tmp_path, chunks=chunks)
    calls: list[int] = []

    def _ner(batch):
        calls.append(len(batch))
        if len(calls) == 1:
            assert cancel_doc_jobs(doc_id) == 1
        return [[EntitySpan(text="Port", type="ORG", start_char=0, end_char=4)] for _ in batch]

    monkeypatch.setattr(entities_stage, "extract_entities_batch", _ner)

    entities_stage.run_entities(doc_id)

    job, _doc, _ = _read(doc_id, JobStage.entities)
    assert calls == [entities_stage.NER_BATCH_SIZE], f"NER kept running after the cancel: {calls}"
    assert job.status == JobStatus.error and job.error == "Cancelled by user"
    session = get_sync_session()
    try:
        written = session.execute(select(Entity).where(Entity.doc_id == doc_id)).scalars().all()
        assert not written, "a cancelled run wrote entities"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_an_uncancelled_entities_run_covers_every_batch(db_session, monkeypatch, tmp_path):
    from harbor_clerk.models.entity import Entity
    from harbor_clerk.worker.ner import EntitySpan
    from harbor_clerk.worker.stages import entities as entities_stage

    chunks = entities_stage.NER_BATCH_SIZE + 3
    doc_id = await _make_doc(db_session, JobStage.entities, tmp_path, chunks=chunks)
    monkeypatch.setattr(
        entities_stage,
        "extract_entities_batch",
        lambda batch: [[EntitySpan(text="Port", type="ORG", start_char=0, end_char=4)] for _ in batch],
    )
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)

    entities_stage.run_entities(doc_id)

    job, _doc, _ = _read(doc_id, JobStage.entities)
    assert job.status == JobStatus.done
    session = get_sync_session()
    try:
        assert len(session.execute(select(Entity).where(Entity.doc_id == doc_id)).scalars().all()) == chunks
    finally:
        session.close()
