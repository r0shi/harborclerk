"""#886 through the chunk stage: the stored chunks of a multi-page document hold
no duplicate tail passage, still cover the whole text, and are numbered without
a gap; and a document that splits into exactly the ceiling's number of chunks is
indexed (the redundant tail used to count against max_chunks_per_document)."""

import hashlib

import pytest
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.stages.chunk import _split_text

PAGES = [" ".join(f"Page {p} sentence {i} of the report." for i in range(40 + 9 * p)) for p in range(1, 6)]


async def _make_doc(db_session, name: str, pages: list[str]):
    doc = Document(
        title=name,
        canonical_filename=f"{name}.txt",
        status="active",
        sha256=hashlib.sha256(name.encode()).digest(),
        source_path=f"/watched/{name}.txt",
        pipeline_status=PipelineStatus.chunking,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    for n, text in enumerate(pages, start=1):
        db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=n, page_text=text))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.running))
    await db_session.commit()
    return doc.doc_id


def _run(doc_id, monkeypatch):
    from harbor_clerk.worker import entry as entry_mod

    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    entry_mod.execute_job(doc_id, JobStage.chunk, 0)

    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.chunk)
        ).scalar_one()
        chunks = session.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_num)).scalars().all()
        return (
            job.status,
            job.error,
            [(c.chunk_num, c.char_start, c.char_end, c.page_start, c.page_end) for c in chunks],
        )
    finally:
        session.close()


@pytest.mark.asyncio
async def test_stored_chunks_of_a_multi_page_document_have_no_tail_duplicate(db_session, monkeypatch):
    doc_id = await _make_doc(db_session, "report", PAGES)
    full_text = "".join(p + "\n" for p in PAGES).rstrip()

    status, error, rows = _run(doc_id, monkeypatch)

    assert status == JobStatus.done, error
    assert len(rows) > 3
    assert [r[0] for r in rows] == list(range(len(rows))), "chunk_num must run 0..n-1 with no gap"
    spans = [(s, e) for _, s, e, _, _ in rows]
    for i, (s1, e1) in enumerate(spans):
        for j, (s2, e2) in enumerate(spans):
            assert i == j or not (s2 <= s1 and e1 <= e2), f"chunk {i} lies inside chunk {j}"
    assert spans[0][0] == 0 and spans[-1][1] == len(full_text), "the whole text is still covered"
    overlap = get_settings().chunk_overlap
    assert all(e1 - s2 == overlap for (_, e1), (s2, _) in zip(spans, spans[1:])), "overlap is as configured"
    assert rows[0][3] == 1 and rows[-1][4] == len(PAGES), "the chunks still span the first to the last page"
    assert sum(1 for r in rows if r[2] == len(full_text)) == 1, "exactly one chunk reaches the end"


@pytest.mark.asyncio
async def test_a_document_of_exactly_the_ceiling_is_indexed(db_session, monkeypatch):
    full_text = "".join(p + "\n" for p in PAGES).rstrip()
    # The distinct passages: a range lying inside another one is not counted, so
    # a duplicate tail chunk would push the document over this ceiling.
    settings = get_settings()
    ranges = _split_text(full_text, target=settings.chunk_target_size, overlap=settings.chunk_overlap)
    n = sum(
        1 for i, (s, e) in enumerate(ranges) if not any(j != i and a <= s and e <= b for j, (a, b) in enumerate(ranges))
    )
    monkeypatch.setenv("MAX_CHUNKS_PER_DOCUMENT", str(n))
    doc_id = await _make_doc(db_session, "at-ceiling", PAGES)

    status, error, rows = _run(doc_id, monkeypatch)

    assert status == JobStatus.done, error
    assert len(rows) == n
