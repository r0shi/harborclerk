"""The embed stage stops when cancelled, defers when the embedder is down, and
the worker bounds its own shutdown.

- #620: cancelling a document did not stop its running embed. The batch loop
  checked only `pipeline_seq`, which a cancel does not bump, so a 57k-chunk
  document kept the embedder at 20-30 GB for 25 minutes after its cancel while
  every counter said idle. SIGTERM waited for the same loop.
- #619: exhausting the embedder client's restart-sized retry budget failed the
  document for good, though the outage (memory pressure) lasted minutes.

Every wait here is bounded: a stage run in a thread is joined with a timeout,
and a stage that would loop is capped.
"""

import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.embedder_client import EmbedderError, EmbedderUnavailableError
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import EMBED_RETRY_MAX_ATTEMPTS, EMBED_RETRY_REASON

EMBED_DIM = 768
BATCH = 64
THREAD_TIMEOUT_S = 30


def _vectors(texts) -> list[list[float]]:
    return [[0.01] * EMBED_DIM for _ in texts]


async def _make_doc(db_session, chunk_count: int, *, job_status=JobStatus.queued, metrics=None, name="embed"):
    doc = Document(
        title=name,
        canonical_filename=f"{name}.txt",
        status="active",
        sha256=hashlib.sha256(name.encode()).digest(),
        pipeline_status=PipelineStatus.embedding,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    for i in range(chunk_count):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=i, chunk_text=f"chunk {i}", language="english"))
    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.embed,
            status=job_status,
            metrics=metrics or {},
            started_at=datetime.now(UTC) if job_status == JobStatus.running else None,
        )
    )
    await db_session.commit()
    return doc.doc_id


def _read(doc_id):
    """(job, doc, embedded chunk count), read fresh."""
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.embed)
        ).scalar_one()
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        embedded = len(
            list(session.execute(select(Chunk.chunk_id).where(Chunk.doc_id == doc_id, Chunk.embedding.isnot(None))))
        )
        session.expunge_all()
        return job, doc, embedded
    finally:
        session.close()


def _run_in_thread(fn, *args):
    """Run fn, bounded by THREAD_TIMEOUT_S; re-raise anything it raised."""
    errors: list[BaseException] = []

    def _target():
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test below
            errors.append(exc)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=THREAD_TIMEOUT_S)
    assert not t.is_alive(), f"stage still running after {THREAD_TIMEOUT_S}s: the cancel did not stop it"
    if errors:
        raise errors[0]


def _clear_queue_style_stop(doc_id):
    """What /system/clear-queue writes: the job row to error, seq untouched."""
    session = get_sync_session()
    try:
        session.execute(
            update(IngestionJob)
            .where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.embed)
            .values(status=JobStatus.error, error="Cancelled by admin (queue clear)")
        )
        session.commit()
    finally:
        session.close()


def _cancel(doc_id):
    from harbor_clerk.worker.pipeline import cancel_doc_jobs

    assert cancel_doc_jobs(doc_id) == 1


# --------------------------------------------------------------------------
# #620 — a cancel stops the running embed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [_cancel, _clear_queue_style_stop], ids=["cancel", "clear-queue"])
async def test_cancel_during_an_embed_call_stops_the_loop_and_discards_that_batch(db_session, monkeypatch, stop):
    """The cancel lands while batch 2 is being encoded.

    The loop must not write batch 2 (the post-call check), must make no third
    call, and must leave the row as the cancel wrote it: the stage still calls
    mark_stage_done on its way out, which must decline an errored row rather
    than mark it done and advance the pipeline the user stopped.
    """
    doc_id = await _make_doc(db_session, chunk_count=BATCH * 4)
    calls: list[int] = []

    def _fake_embed(texts, *, task, timeout):
        calls.append(len(texts))
        if len(calls) == 2:
            stop(doc_id)
        return _vectors(texts)

    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _fake_embed)
    from harbor_clerk.worker.stages.embed import run_embed

    _run_in_thread(run_embed, doc_id)

    job, _doc, embedded = _read(doc_id)
    assert calls == [BATCH, BATCH], f"the loop kept calling the embedder after the cancel: {calls}"
    assert embedded == BATCH, f"the batch encoded across the cancel was written: {embedded} chunks"
    assert job.status == JobStatus.error, f"the cancelled row became {job.status}"
    assert job.error.startswith("Cancelled by"), job.error


@pytest.mark.asyncio
async def test_cancel_between_batches_makes_no_further_embedder_call(db_session, monkeypatch):
    """The cancel lands after batch 1 is stored: batch 2 must not be sent at all.

    A check only before the write would still send it, burning an encode (and,
    on the night of #620, gigabytes of embedder memory) on a cancelled document.
    """
    doc_id = await _make_doc(db_session, chunk_count=BATCH * 3)
    calls: list[int] = []

    def _fake_embed(texts, *, task, timeout):
        calls.append(len(texts))
        return _vectors(texts)

    def _publish(doc_id_, stage, status, **fields):
        if fields.get("progress") == BATCH:
            _cancel(doc_id)

    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _fake_embed)
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.publish_job_event", _publish)
    from harbor_clerk.worker.stages.embed import run_embed

    _run_in_thread(run_embed, doc_id)

    job, doc, embedded = _read(doc_id)
    assert calls == [BATCH], f"an embedder call was made after the cancel: {calls}"
    assert embedded == BATCH
    assert job.status == JobStatus.error
    assert doc.pipeline_status == PipelineStatus.error


@pytest.mark.asyncio
async def test_uncancelled_embed_still_completes(db_session, monkeypatch):
    """The checks must not stop a run nobody stopped (job row `running`, as a worker claims it)."""
    doc_id = await _make_doc(db_session, chunk_count=BATCH * 2 + 3, job_status=JobStatus.running)
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", lambda texts, *, task, timeout: _vectors(texts))
    from harbor_clerk.worker.stages.embed import run_embed

    _run_in_thread(run_embed, doc_id)

    job, _doc, embedded = _read(doc_id)
    assert embedded == BATCH * 2 + 3
    assert job.status == JobStatus.done


# --------------------------------------------------------------------------
# #619 — an embedder outage defers the embed instead of failing the document
# --------------------------------------------------------------------------


def _execute_embed(doc_id):
    from harbor_clerk.worker.entry import execute_job

    execute_job(doc_id, JobStage.embed, 0)


def _raise_unavailable(texts, *, task, timeout):
    raise EmbedderUnavailableError("embedder call failed after 7/7 attempts: HTTP 503")


@pytest.mark.asyncio
async def test_unavailable_embedder_defers_the_job_instead_of_failing_it(db_session, monkeypatch):
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, chunk_count=3, job_status=JobStatus.running)
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _raise_unavailable)

    before = datetime.now(UTC)
    _execute_embed(doc_id)

    job, doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued, f"an outage failed the job: {job.status} {job.error}"
    assert job.error is None
    assert job.started_at is None and job.heartbeat_at is None
    assert job.metrics["reason"] == EMBED_RETRY_REASON
    assert job.metrics["retry_attempts"] == 1
    assert "HTTP 503" in job.metrics["last_error"]
    assert datetime.fromisoformat(job.metrics["retry_after"]) > before + timedelta(seconds=30)
    assert doc.pipeline_status != PipelineStatus.error, "the document was failed for an embedder outage"

    # Not claimable until retry_after: claiming now re-runs it into the same outage.
    assert entry_mod.claim_next_job([JobStage.embed]) is None

    session = get_sync_session()
    try:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        session.execute(
            update(IngestionJob)
            .where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.embed)
            .values(metrics={**job.metrics, "retry_after": past})
        )
        session.commit()
    finally:
        session.close()
    claimed = entry_mod.claim_next_job([JobStage.embed])
    assert claimed is not None and claimed[0] == doc_id, "a due retry was not claimed"


@pytest.mark.asyncio
async def test_unavailable_embedder_fails_the_document_once_the_budget_is_spent(db_session, monkeypatch):
    doc_id = await _make_doc(
        db_session,
        chunk_count=3,
        job_status=JobStatus.running,
        metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": EMBED_RETRY_MAX_ATTEMPTS - 1},
    )
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _raise_unavailable)

    _execute_embed(doc_id)

    job, doc, _ = _read(doc_id)
    assert job.status == JobStatus.error, f"retries never end: {job.status}"
    assert job.error.startswith("Embedder unavailable: gave up after"), job.error
    assert "HTTP 503" in job.error, "the reason must name the last failure"
    assert job.metrics.get("retry_exhausted") is True
    assert doc.pipeline_status == PipelineStatus.error
    assert doc.error == job.error


@pytest.mark.asyncio
async def test_an_attempt_that_made_progress_restarts_the_retry_count(db_session, monkeypatch):
    """A slow document that keeps storing batches is not a broken one."""
    doc_id = await _make_doc(
        db_session,
        chunk_count=BATCH * 2,
        job_status=JobStatus.running,
        metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": EMBED_RETRY_MAX_ATTEMPTS - 1},
    )
    calls: list[int] = []

    def _first_ok_then_down(texts, *, task, timeout):
        calls.append(len(texts))
        if len(calls) == 1:
            return _vectors(texts)
        raise EmbedderUnavailableError("embedder call failed after 7/7 attempts: HTTP 503")

    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _first_ok_then_down)

    _execute_embed(doc_id)

    job, _doc, embedded = _read(doc_id)
    assert embedded == BATCH
    assert job.status == JobStatus.queued, f"progressing document was failed: {job.error}"
    assert job.metrics["retry_attempts"] == 1


@pytest.mark.asyncio
async def test_a_rejected_request_still_fails_at_once(db_session, monkeypatch):
    """A 4xx says the request is wrong; waiting changes nothing."""
    doc_id = await _make_doc(db_session, chunk_count=3, job_status=JobStatus.running)

    def _rejected(texts, *, task, timeout):
        raise EmbedderError("embedder rejected request: HTTP 422")

    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _rejected)

    _execute_embed(doc_id)

    job, doc, _ = _read(doc_id)
    assert job.status == JobStatus.error
    assert "HTTP 422" in job.error
    assert doc.pipeline_status == PipelineStatus.error


@pytest.mark.asyncio
async def test_a_cancel_during_the_failing_call_is_not_turned_into_a_retry(db_session, monkeypatch):
    doc_id = await _make_doc(db_session, chunk_count=3, job_status=JobStatus.running)

    def _cancel_then_fail(texts, *, task, timeout):
        _cancel(doc_id)
        raise EmbedderUnavailableError("embedder call failed after 7/7 attempts: HTTP 503")

    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", _cancel_then_fail)

    _execute_embed(doc_id)

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.error
    assert job.error == "Cancelled by user", f"the cancel was overwritten: {job.error}"
    assert "retry_after" not in (job.metrics or {})


@pytest.mark.asyncio
async def test_re_enqueue_starts_the_retry_budget_afresh(db_session):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    doc_id = await _make_doc(
        db_session,
        chunk_count=1,
        job_status=JobStatus.error,
        metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": 6, "retry_after": future},
    )
    from harbor_clerk.worker.pipeline import enqueue_stage

    enqueue_stage(doc_id, JobStage.embed)

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued
    assert job.metrics == {}, f"a fresh cycle inherited retry state: {job.metrics}"


# --------------------------------------------------------------------------
# #620 — SIGTERM no longer waits for the whole job
# --------------------------------------------------------------------------


def _stubborn_stage(*, on_start=None, cap_seconds: float = 15.0):
    """A stage that runs until stopped, swallowing ordinary exceptions as a stage might."""

    def _stage(doc_id, *, worker_seq=None):
        if on_start is not None:
            on_start()
        deadline = time.monotonic() + cap_seconds
        while time.monotonic() < deadline:
            try:
                time.sleep(0.05)
            except Exception:  # noqa: BLE001 — the point: an `except Exception` must not absorb the deadline
                pass

    return _stage


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["during", "before"])
async def test_sigterm_abandons_and_requeues_the_running_job_after_the_grace(db_session, monkeypatch, when):
    import signal

    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running)
    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 1)
    monkeypatch.setattr(entry_mod, "_shutdown", False)
    if when == "during":
        stage = _stubborn_stage(on_start=lambda: entry_mod._handle_signal(signal.SIGTERM, None))
    else:
        # SIGTERM between the claim and execute_job: the job must be bounded too.
        monkeypatch.setattr(entry_mod, "_shutdown", True)
        stage = _stubborn_stage()
    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, stage)

    started = time.monotonic()
    entry_mod.execute_job(doc_id, JobStage.embed, 0)
    elapsed = time.monotonic() - started

    assert elapsed < 6, f"shutdown waited {elapsed:.1f}s for the job; the grace is 1s"
    job, doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued, f"abandoned job left {job.status}: {job.error}"
    assert job.started_at is None
    assert doc.pipeline_status != PipelineStatus.error, "an interrupted job is not a failed one"
    assert entry_mod._shutdown_deadline_armed is False, "the deadline must not outlive the job"
