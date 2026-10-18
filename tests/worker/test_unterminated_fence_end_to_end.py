"""The seventh review's end-to-end case: an unterminated code fence must not fail a document.

`_split_text` never ended a chunk inside a fence, and an unterminated fence runs
to the end of the document, so a ~350k-character file became one chunk. The
embedder answered 422, the worker treats a 4xx as final, and the document went
to error with an opaque "HTTP 422". The chunker now caps every chunk at
CHUNK_HARD_MAX_FACTOR x the target, the embedder no longer refuses content, and
a rejection carries the embedder's reason.

The embedder here is a stand-in with the old, strict contract (a 422 for any
text over 200k characters), so the test proves the chunker alone keeps the
document ingestible.
"""

import hashlib
import json

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

EMBED_URL = "http://embedder:8000/embed"
STRICT_MAX_CHARS = 200_000


def _strict_embedder(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["texts"]
    too_long = [len(t) for t in texts if len(t) > STRICT_MAX_CHARS]
    if too_long:
        return httpx.Response(422, json={"detail": f"input rejected: text is {too_long[0]:,} characters"})
    return httpx.Response(200, json={"embeddings": [[0.01] * 768 for _ in texts]})


@pytest.mark.asyncio
async def test_an_unterminated_fence_document_chunks_embeds_and_finishes(
    db_session, httpx_mock: HTTPXMock, monkeypatch
):
    from harbor_clerk.worker import entry as entry_mod

    page_text = "Sample export.\n\n~~~\n" + "".join(f'{{"row": {i}, "value": "sample-{i}"}}\n' for i in range(9_300))
    assert len(page_text) > 340_000

    doc = Document(
        title="export",
        canonical_filename="export.md",
        status="active",
        sha256=hashlib.sha256(b"unterminated-fence").digest(),
        source_path="/watched/export.md",
        mime_type="text/markdown",
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=page_text))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.running))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    httpx_mock.add_callback(_strict_embedder, url=EMBED_URL, is_reusable=True)

    entry_mod.execute_job(doc_id, JobStage.chunk, 0)
    entry_mod.execute_job(doc_id, JobStage.embed, 0)

    session = get_sync_session()
    try:
        jobs = {
            j.stage: j for j in session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id)).scalars()
        }
        chunks = session.execute(select(Chunk).where(Chunk.doc_id == doc_id)).scalars().all()
        assert jobs[JobStage.chunk].status == JobStatus.done, jobs[JobStage.chunk].error
        assert jobs[JobStage.embed].status == JobStatus.done, jobs[JobStage.embed].error
        assert len(chunks) > 1, "the fence was not split"
        assert all(c.embedding is not None for c in chunks), "some chunks were left without a vector"
        assert max(len(c.chunk_text) for c in chunks) <= 4_000
    finally:
        session.close()
