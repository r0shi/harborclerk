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


async def _make_doc(db_session, stage: JobStage, tmp_path, *, pages: int = 0, chunks: int = 0):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 stand-in; rendering is stubbed")
    doc = Document(
        title="scan",
        canonical_filename="scan.pdf",
        status="active",
        sha256=hashlib.sha256(stage.value.encode()).digest(),
        source_path=str(source),
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
