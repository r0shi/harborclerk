"""The #552 regression: a transient embedder failure must not fail the stage.

Before the retry landed, a single connection blip mid-batch left the document
`error` and needed a manual reprocess. These tests drive the real `run_embed`
against a mocked embedder so the assertion is about the *stage outcome* — job
done, chunks embedded — not just about the HTTP helper.
"""

import hashlib

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

EMBED_URL = "http://embedder:8000/embed"
EMBED_DIM = 768


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("harbor_clerk.embedder_client.time.sleep", lambda _s: None)


def _vectors(n: int) -> dict:
    """An embedder response for `n` texts, at the real stored dimension."""
    return {"embeddings": [[0.01] * EMBED_DIM for _ in range(n)]}


async def _make_doc_with_chunks(db_session, chunk_count: int):
    doc = Document(
        title="retry-fixture",
        canonical_filename="retry-fixture.txt",
        status="active",
        sha256=hashlib.sha256(b"retry-fixture").digest(),
        pipeline_status=PipelineStatus.chunked,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()

    for i in range(chunk_count):
        db_session.add(
            Chunk(
                doc_id=doc.doc_id,
                chunk_num=i,
                chunk_text=f"chunk number {i}",
                language="english",
            )
        )
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.queued))
    await db_session.commit()
    return doc.doc_id


def _stage_outcome(doc_id):
    """Read back the job row and how many chunks got vectors."""
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == JobStage.embed,
            )
        ).scalar_one()
        embedded = session.execute(select(Chunk).where(Chunk.doc_id == doc_id, Chunk.embedding.isnot(None))).scalars()
        return job.status, len(list(embedded))
    finally:
        session.close()


@pytest.mark.asyncio
async def test_embed_stage_survives_transient_embedder_failure(db_session, httpx_mock: HTTPXMock):
    """Two transient failures then success — the stage still completes."""
    doc_id = await _make_doc_with_chunks(db_session, chunk_count=3)

    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=EMBED_URL)
    httpx_mock.add_response(url=EMBED_URL, status_code=503)
    httpx_mock.add_response(url=EMBED_URL, json=_vectors(3))

    from harbor_clerk.worker.stages.embed import run_embed

    run_embed(doc_id)

    status, embedded = _stage_outcome(doc_id)
    assert status == JobStatus.done, f"stage should complete despite transient failures, got {status}"
    assert embedded == 3, f"all chunks should be embedded, got {embedded}"
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_embed_stage_raises_when_embedder_is_truly_down(db_session, httpx_mock: HTTPXMock):
    """Retry must not paper over a real outage.

    Asserts what `run_embed` actually controls: it raises EmbedderError and
    writes no embeddings. It deliberately does NOT assert the job row is
    `error` — recording that is `execute_job`'s job (worker/entry.py), not
    `run_embed`'s, and `mark_stage_running` never sets `running` either, so
    the row is still `queued` here. An earlier version of this test claimed
    otherwise and asserted neither.
    """
    doc_id = await _make_doc_with_chunks(db_session, chunk_count=2)

    from harbor_clerk.embedder_client import DEFAULT_MAX_ATTEMPTS, EmbedderError
    from harbor_clerk.worker.stages.embed import run_embed

    for _ in range(DEFAULT_MAX_ATTEMPTS):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    with pytest.raises(EmbedderError):
        run_embed(doc_id)

    _status, embedded = _stage_outcome(doc_id)
    assert embedded == 0
    assert len(httpx_mock.get_requests()) == DEFAULT_MAX_ATTEMPTS
