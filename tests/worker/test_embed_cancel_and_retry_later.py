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
async def test_a_successful_embed_clears_the_retry_state_of_earlier_deferrals(db_session, monkeypatch):
    """A done row must not keep "embedder_unavailable", the attempt count and the last error."""
    deferred = {
        "reason": EMBED_RETRY_REASON,
        "retry_attempts": 3,
        "retry_after": "2026-01-01T00:00:00+00:00",
        "last_error": "EmbedderUnavailableError: HTTP 503",
        "last_failed_at": "2026-01-01T00:00:00+00:00",
    }
    doc_id = await _make_doc(db_session, chunk_count=2, job_status=JobStatus.running, metrics=deferred)
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", lambda texts, *, task, timeout: _vectors(texts))
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    from harbor_clerk.worker.stages.embed import run_embed

    _run_in_thread(run_embed, doc_id)

    job, _doc, embedded = _read(doc_id)
    assert embedded == 2 and job.status == JobStatus.done
    assert job.metrics == {}, f"a done embed kept stale failure state: {job.metrics}"


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


def _rejected_after_cancel(doc_id):
    def _fail(texts, *, task, timeout):
        _cancel(doc_id)
        raise EmbedderError("embedder rejected request: HTTP 422")

    return _fail


def _timed_out_after_cancel(doc_id):
    def _fail(texts, *, task, timeout):
        _cancel(doc_id)
        raise TimeoutError("Stage embed timed out after 3600s")

    return _fail


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_rejected_after_cancel, _timed_out_after_cancel], ids=["422", "stage-timeout"])
async def test_a_cancel_survives_an_ordinary_failure_that_follows_it(db_session, monkeypatch, failure):
    """The ordinary failure path wrote the failure over the cancel: cancel an embed,
    then hit a 422 (or the stage timeout), and the row and document read
    "EmbedderError … HTTP 422" instead of "Cancelled by user"."""
    doc_id = await _make_doc(db_session, chunk_count=3, job_status=JobStatus.running)
    monkeypatch.setattr("harbor_clerk.worker.stages.embed.embed_texts", failure(doc_id))

    _execute_embed(doc_id)

    job, doc, _ = _read(doc_id)
    assert (job.status, job.error) == (JobStatus.error, "Cancelled by user"), job.error
    assert (doc.pipeline_status, doc.error) == (PipelineStatus.error, "Cancelled by user"), doc.error


@pytest.mark.asyncio
async def test_a_cancel_survives_an_apple_intelligence_failure_that_follows_it(db_session, monkeypatch):
    """The summarize sibling: AFM unavailable after a cancel used to schedule a retry."""
    from harbor_clerk.llm.summarize import AppleIntelligenceUnavailableError
    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="afm-cancel",
        canonical_filename="afm-cancel.txt",
        status="active",
        sha256=hashlib.sha256(b"afm-cancel").digest(),
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.running))
    await db_session.commit()
    doc_id = doc.doc_id

    def _stage(doc_id_, *, worker_seq=None):
        _cancel_stage(doc_id_)
        raise AppleIntelligenceUnavailableError("Apple Intelligence summaries are enabled but unavailable")

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.summarize, _stage)

    entry_mod.execute_job(doc_id, JobStage.summarize, 0)

    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert (job.status, job.error) == (JobStatus.error, "Cancelled by user")
        assert "retry_after" not in (job.metrics or {}), "a cancel was turned into a scheduled retry"
    finally:
        session.close()


def _cancel_stage(doc_id):
    from harbor_clerk.worker.pipeline import cancel_doc_jobs

    assert cancel_doc_jobs(doc_id) >= 1


@pytest.mark.asyncio
async def test_unavailable_from_a_non_embed_stage_fails_it_rather_than_deferring_embed(db_session, monkeypatch):
    """Only embed has the retry-later schedule. Another stage raising
    EmbedderUnavailableError fails that stage; deferring would write the embed row
    and leave the raising stage's own row running."""
    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="chunk-unavailable",
        canonical_filename="chunk-unavailable.txt",
        status="active",
        sha256=hashlib.sha256(b"chunk-unavailable").digest(),
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.running))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    def _stage(doc_id_, *, worker_seq=None):
        raise EmbedderUnavailableError("embedder call failed after 7/7 attempts: HTTP 503")

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.chunk, _stage)

    entry_mod.execute_job(doc_id, JobStage.chunk, 0)

    session = get_sync_session()
    try:
        rows = {
            j.stage: j for j in session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id)).scalars()
        }
        assert rows[JobStage.chunk].status == JobStatus.error, rows[JobStage.chunk].status
        assert rows[JobStage.embed].status == JobStatus.queued
        assert "retry_after" not in (rows[JobStage.embed].metrics or {}), "the embed row was deferred instead"
    finally:
        session.close()


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

    # A job claimed after an earlier deferral still carries that schedule.
    stale = {"reason": EMBED_RETRY_REASON, "retry_attempts": 2, "retry_after": "2026-01-01T00:00:00+00:00"}
    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running, metrics=stale)
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
    # Requeued for a shutdown, not an outage: no stale "waiting on the embedder, next retry <past>".
    assert "reason" not in job.metrics and "retry_after" not in job.metrics, job.metrics
    assert job.metrics["retry_attempts"] == 2, "an interruption is not progress"


@pytest.mark.asyncio
async def test_the_shutdown_requeue_announces_the_job_like_an_enqueue(db_session, monkeypatch):
    """It published no event and no pg_notify: the UI kept showing the job running,
    and other workers found it only at their 30 s idle poll."""
    import select as select_mod
    import signal

    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running)
    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 1)
    monkeypatch.setattr(entry_mod, "_shutdown", False)
    monkeypatch.setitem(
        entry_mod.STAGE_FUNCTIONS,
        JobStage.embed,
        _stubborn_stage(on_start=lambda: entry_mod._handle_signal(signal.SIGTERM, None)),
    )
    events: list[tuple] = []
    real_publish = entry_mod.publish_job_event

    def _capture(doc, stage, status, **fields):
        events.append((doc, stage, status))
        return real_publish(doc, stage, status, **fields)

    monkeypatch.setattr(entry_mod, "publish_job_event", _capture)

    listen = entry_mod._get_listen_connection()
    try:
        with listen.cursor() as cur:
            cur.execute("LISTEN job_enqueued_cpu")

        entry_mod.execute_job(doc_id, JobStage.embed, 0)

        payloads: list[str] = []
        if select_mod.select([listen], [], [], 5) != ([], [], []):
            listen.poll()
            payloads = [n.payload for n in listen.notifies]
    finally:
        listen.close()

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued
    assert str(doc_id) in payloads, f"no pg_notify on the embed stage's queue: {payloads}"
    assert (doc_id, "embed", "queued") in events, f"no queued job event: {events}"


def test_a_stage_timeout_due_within_the_grace_is_left_to_fire(monkeypatch):
    """When the stage's own timeout comes sooner than the grace, SIGTERM leaves it
    alone: re-arming to the grace would extend the stage past its own deadline."""
    import signal

    from harbor_clerk.worker import entry as entry_mod

    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 8)
    monkeypatch.setattr(entry_mod, "_shutdown_deadline_armed", False)
    previous = signal.signal(signal.SIGALRM, lambda *_: None)  # nothing may fire into the test
    try:
        signal.alarm(3)  # the stage timeout, 3 s away
        entry_mod._arm_shutdown_deadline()
        remaining = signal.alarm(0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert entry_mod._shutdown_deadline_armed is False, "the deadline replaced a sooner stage timeout"
    assert 1 <= remaining <= 3, f"the stage timeout was moved to {remaining}s"


def test_a_stage_timeout_due_after_the_grace_is_replaced_by_it(monkeypatch):
    import signal

    from harbor_clerk.worker import entry as entry_mod

    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 8)
    monkeypatch.setattr(entry_mod, "_shutdown_deadline_armed", False)
    previous = signal.signal(signal.SIGALRM, lambda *_: None)
    try:
        signal.alarm(3600)
        entry_mod._arm_shutdown_deadline()
        remaining = signal.alarm(0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert entry_mod._shutdown_deadline_armed is True
    assert 7 <= remaining <= 8, f"expected the 8 s grace, got {remaining}s"


# --------------------------------------------------------------------------
# The alarm cannot fire into the recording of a stage's outcome
# --------------------------------------------------------------------------


def _early_alarm():
    """Deliver SIGALRM now, as if the deadline fired at exactly this point."""
    import signal

    signal.raise_signal(signal.SIGALRM)


@pytest.mark.asyncio
async def test_a_deadline_between_done_and_advance_does_not_strand_the_document(db_session, monkeypatch):
    """mark_stage_done commits `done`, then advances. A deadline between the two left
    the job done with nothing enqueued after it, and the requeue matches only
    `running` rows: the document stranded."""
    import signal

    from harbor_clerk.worker import entry as entry_mod
    from harbor_clerk.worker.pipeline import mark_stage_done

    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running)
    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 1)
    monkeypatch.setattr(entry_mod, "_shutdown", False)
    advanced: list[bool] = []

    def _advance(doc_id_):
        _early_alarm()  # the deadline lands after `done` is committed
        advanced.append(True)

    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", _advance)

    def _stage(doc_id_, *, worker_seq=None):
        entry_mod._handle_signal(signal.SIGTERM, None)  # arms the shutdown deadline
        mark_stage_done(doc_id_, JobStage.embed, worker_seq=worker_seq)

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, _stage)

    entry_mod.execute_job(doc_id, JobStage.embed, 0)

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.done
    assert advanced == [True], "the deadline interrupted the pipeline advance after `done` was committed"


@pytest.mark.asyncio
async def test_a_deadline_during_failure_bookkeeping_does_not_kill_the_worker(db_session, monkeypatch):
    """Inside execute_job's handlers (here the retry-later write) a deadline used to
    escape as _ShutdownDeadline and take the worker down mid-write."""
    import signal

    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running)
    monkeypatch.setattr(entry_mod, "SHUTDOWN_GRACE_SECONDS", 1)
    monkeypatch.setattr(entry_mod, "_shutdown", False)
    real_record = entry_mod._record_embed_unavailable_job

    def _record(*args, **kwargs):
        _early_alarm()
        return real_record(*args, **kwargs)

    monkeypatch.setattr(entry_mod, "_record_embed_unavailable_job", _record)

    def _stage(doc_id_, *, worker_seq=None):
        entry_mod._handle_signal(signal.SIGTERM, None)
        raise EmbedderUnavailableError("embedder call failed after 7/7 attempts: HTTP 503")

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, _stage)

    entry_mod.execute_job(doc_id, JobStage.embed, 0)  # must not raise

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued
    assert job.metrics["reason"] == EMBED_RETRY_REASON, "the deferral was not recorded"


# --------------------------------------------------------------------------
# The reaper's requeue drops a stale deferral too
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_requeue_clears_a_stale_embed_deferral(db_session, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    import harbor_clerk.api.app as app_mod

    stale = {"reason": EMBED_RETRY_REASON, "retry_attempts": 3, "retry_after": "2026-01-01T00:00:00+00:00"}
    doc_id = await _make_doc(db_session, chunk_count=1, job_status=JobStatus.running, metrics=stale)
    # A summarize orphan whose retry_after is its Apple Intelligence backoff: not ours to clear.
    afm = {"reason": "apple_intelligence_unavailable", "retry_attempts": 2, "retry_after": "2026-01-01T00:05:00+00:00"}
    summary_doc = Document(
        title="afm",
        canonical_filename="afm.txt",
        status="active",
        sha256=hashlib.sha256(b"afm-orphan").digest(),
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(summary_doc)
    await db_session.flush()
    db_session.add(
        IngestionJob(doc_id=summary_doc.doc_id, stage=JobStage.summarize, status=JobStatus.running, metrics=afm)
    )
    await db_session.flush()
    await db_session.execute(
        update(IngestionJob)
        .where(IngestionJob.doc_id.in_([doc_id, summary_doc.doc_id]))
        .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=10))
    )
    await db_session.commit()
    summary_doc_id = summary_doc.doc_id

    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    class _Stop(BaseException):
        pass

    sleeps = iter([None])

    async def _sleep(_seconds):
        if next(sleeps, "stop") == "stop":
            raise _Stop()

    from types import SimpleNamespace

    monkeypatch.setattr("harbor_clerk.db.async_session_factory", _factory)
    # Only the loop's own `asyncio.sleep` (its sole asyncio use), not the event loop's.
    monkeypatch.setattr(app_mod, "asyncio", SimpleNamespace(sleep=_sleep))

    with pytest.raises(_Stop):
        await asyncio.wait_for(app_mod._session_reaper_loop(), timeout=30)
    # The reaper's session joined the test connection's transaction; make it visible.
    await db_session.commit()

    job, _doc, _ = _read(doc_id)
    assert job.status == JobStatus.queued, "the orphan was not requeued"
    assert "reason" not in job.metrics and "retry_after" not in job.metrics, job.metrics
    assert job.metrics["retry_attempts"] == 3

    session = get_sync_session()
    try:
        summary_job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == summary_doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert summary_job.status == JobStatus.queued
        assert summary_job.metrics == afm, f"a summarize row's AFM schedule was cleared: {summary_job.metrics}"
    finally:
        session.close()


def test_the_schedule_helper_touches_embed_rows_only():
    from harbor_clerk.worker.pipeline import without_embed_retry_schedule

    afm = {"reason": "apple_intelligence_unavailable", "retry_after": "2026-01-01T00:05:00+00:00"}
    deferred = {"reason": EMBED_RETRY_REASON, "retry_attempts": 2, "retry_after": "2026-01-01T00:05:00+00:00"}

    assert without_embed_retry_schedule(afm, JobStage.summarize) == afm
    assert without_embed_retry_schedule(deferred, JobStage.embed) == {"retry_attempts": 2}
