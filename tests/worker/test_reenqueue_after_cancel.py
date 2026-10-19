"""A cancelled run never writes over the run the user asked for next (#862 review 8).

A cancel sets the job row to `error`; Retry (`enqueue_stage`) then puts the same
generation's row back to `queued` and clears its `started_at`. The cancelled
run is still executing, and every write it makes on its own behalf (done, a
failure, a deferral, a shutdown requeue, a heartbeat, finalize's `ready`) must
see that the row is a newer claim's and leave it alone. Before, only `done`
did: a RuntimeError marked the user's retry `error`, and an embedder outage gave
the fresh row a retry schedule a minute out.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.embedder_client import EmbedderUnavailableError
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import cancel_doc_jobs, enqueue_stage


async def _make_doc(db_session, stage: JobStage, *, pipeline_status=PipelineStatus.embedding, name=None):
    """A document whose `stage` row is running under a claim (started_at stamped)."""
    name = name or f"reenqueue-{stage.value}"
    doc = Document(
        title=name,
        canonical_filename=f"{name}.txt",
        status="active",
        sha256=hashlib.sha256(name.encode()).digest(),
        pipeline_status=pipeline_status,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.running, started_at=datetime.now(UTC)))
    await db_session.commit()
    return doc.doc_id


def _read(doc_id, stage: JobStage):
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == stage)
        ).scalar_one()
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        session.expunge_all()
        return job, doc
    finally:
        session.close()


def _cancel_then_retry(doc_id, stage: JobStage) -> None:
    """The user cancels, then clicks Retry: same generation, row queued again."""
    assert cancel_doc_jobs(doc_id) == 1
    enqueue_stage(doc_id, stage)


def _claimed(doc_id, stage: JobStage):
    session = get_sync_session()
    try:
        return session.execute(
            select(IngestionJob.started_at).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == stage)
        ).scalar_one()
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), EmbedderUnavailableError("HTTP 503")],
    ids=["failure", "embedder-unavailable"],
)
async def test_a_cancelled_runs_failure_leaves_the_re_enqueued_row_alone(db_session, monkeypatch, exc):
    """The reviewer's repro: the old run fails after the cancel and the Retry."""
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, JobStage.embed)
    claim = _claimed(doc_id, JobStage.embed)

    def _stage(doc_id_, *, worker_seq=None):
        _cancel_then_retry(doc_id_, JobStage.embed)
        raise exc

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, _stage)
    entry_mod.execute_job(doc_id, JobStage.embed, 0, claim)

    job, doc = _read(doc_id, JobStage.embed)
    assert job.status == JobStatus.queued, f"the re-enqueued row was overwritten: {job.status} {job.error}"
    assert job.error is None
    assert not (job.metrics or {}).get("retry_after"), "the re-enqueued row was given this run's retry schedule"
    assert doc.pipeline_status != PipelineStatus.error and doc.error is None, doc.error


@pytest.mark.asyncio
async def test_a_cancelled_runs_apple_intelligence_failure_leaves_the_re_enqueued_row_alone(db_session, monkeypatch):
    from harbor_clerk.llm.summarize import AppleIntelligenceUnavailableError
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, JobStage.summarize, pipeline_status=PipelineStatus.ready)
    claim = _claimed(doc_id, JobStage.summarize)

    def _stage(doc_id_, *, worker_seq=None):
        _cancel_then_retry(doc_id_, JobStage.summarize)
        raise AppleIntelligenceUnavailableError("Apple Intelligence summaries are enabled but unavailable")

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.summarize, _stage)
    entry_mod.execute_job(doc_id, JobStage.summarize, 0, claim)

    job, _doc = _read(doc_id, JobStage.summarize)
    assert job.status == JobStatus.queued and job.error is None, (job.status, job.error)
    assert not (job.metrics or {}).get("retry_after"), "the re-enqueued row was given this run's AFM schedule"
    assert not (job.metrics or {}).get("retry_attempts"), job.metrics


@pytest.mark.asyncio
async def test_the_shutdown_requeue_never_hands_back_another_workers_live_claim(db_session, monkeypatch):
    """Cancel, Retry, and a second worker claims the row; the first run then hits
    the shutdown deadline. Its requeue matched any running row at the
    generation, and would have put the second worker's live run back to queued."""
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, JobStage.embed)
    claim = _claimed(doc_id, JobStage.embed)
    second_claim = claim + timedelta(seconds=5)

    def _stage(doc_id_, *, worker_seq=None):
        _cancel_then_retry(doc_id_, JobStage.embed)
        session = get_sync_session()
        try:
            job = session.execute(
                select(IngestionJob).where(IngestionJob.doc_id == doc_id_, IngestionJob.stage == JobStage.embed)
            ).scalar_one()
            job.status, job.started_at = JobStatus.running, second_claim  # what claim_next_job writes
            session.commit()
        finally:
            session.close()
        raise entry_mod._ShutdownDeadline()

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, _stage)
    entry_mod.execute_job(doc_id, JobStage.embed, 0, claim)

    job, _doc = _read(doc_id, JobStage.embed)
    assert job.status == JobStatus.running, "the second worker's live claim was requeued"
    assert job.started_at == second_claim


@pytest.mark.asyncio
async def test_the_shutdown_requeue_still_requeues_its_own_claim(db_session, monkeypatch):
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, JobStage.embed)
    claim = _claimed(doc_id, JobStage.embed)

    def _stage(doc_id_, *, worker_seq=None):
        raise entry_mod._ShutdownDeadline()

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.embed, _stage)
    entry_mod.execute_job(doc_id, JobStage.embed, 0, claim)

    job, _doc = _read(doc_id, JobStage.embed)
    assert job.status == JobStatus.queued and job.started_at is None


class _OneBeat:
    """A stop event that lets _heartbeat_loop beat exactly once."""

    def __init__(self):
        self.calls = 0

    def wait(self, timeout=None):
        self.calls += 1
        return self.calls > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("re_enqueued", [False, True], ids=["own-claim", "re-enqueued"])
async def test_the_heartbeat_beats_only_for_its_own_claim(db_session, re_enqueued):
    from harbor_clerk.worker import entry as entry_mod

    doc_id = await _make_doc(db_session, JobStage.embed, name=f"heartbeat-{re_enqueued}")
    claim = _claimed(doc_id, JobStage.embed)
    if re_enqueued:
        _cancel_then_retry(doc_id, JobStage.embed)

    entry_mod._heartbeat_loop(doc_id, JobStage.embed, 0, _OneBeat(), claim)

    job, _doc = _read(doc_id, JobStage.embed)
    if re_enqueued:
        assert job.heartbeat_at is None, "a cancelled run kept the re-enqueued row's heartbeat fresh"
    else:
        assert job.heartbeat_at is not None, "the heartbeat did not beat for its own claim"


@pytest.mark.asyncio
async def test_claim_next_job_returns_the_claims_started_at(db_session, monkeypatch):
    """execute_job's claim identity comes from the claim's own transaction, not a later read."""
    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="claim-identity",
        canonical_filename="claim-identity.txt",
        status="active",
        sha256=hashlib.sha256(b"claim-identity").digest(),
        pipeline_status=PipelineStatus.embedding,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.queued))
    await db_session.commit()

    claimed = entry_mod.claim_next_job([JobStage.embed])

    assert claimed is not None and claimed[0] == doc.doc_id
    assert len(claimed) == 4
    assert claimed[3] is not None and claimed[3] == _claimed(doc.doc_id, JobStage.embed)


@pytest.mark.asyncio
@pytest.mark.parametrize("re_enqueued", [False, True], ids=["uninterrupted", "cancel-then-retry"])
async def test_finalize_writes_ready_only_for_its_own_claim(db_session, monkeypatch, re_enqueued):
    """finalize sets pipeline_status itself, before mark_stage_done: it checked only
    the seq, so a cancelled-then-retried finalize still read `ready`."""
    from harbor_clerk.worker import entry as entry_mod
    from harbor_clerk.worker.stages import finalize as finalize_stage

    doc_id = await _make_doc(
        db_session, JobStage.finalize, pipeline_status=PipelineStatus.finalizing, name=f"finalize-{re_enqueued}"
    )
    claim = _claimed(doc_id, JobStage.finalize)
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _doc_id: None)
    real_running = finalize_stage.mark_stage_running

    def _running(doc_id_, stage, *, worker_seq=None):
        ok = real_running(doc_id_, stage, worker_seq=worker_seq)
        if re_enqueued:
            _cancel_then_retry(doc_id_, JobStage.finalize)
        return ok

    monkeypatch.setattr(finalize_stage, "mark_stage_running", _running)
    entry_mod.execute_job(doc_id, JobStage.finalize, 0, claim)

    job, doc = _read(doc_id, JobStage.finalize)
    if re_enqueued:
        assert doc.pipeline_status != PipelineStatus.ready, "a cancelled finalize marked the document ready"
        assert job.status == JobStatus.queued
    else:
        assert doc.pipeline_status == PipelineStatus.ready, (doc.pipeline_status, doc.error)
        assert job.status == JobStatus.done


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), EmbedderUnavailableError("HTTP 503")],
    ids=["failure", "embedder-unavailable"],
)
async def test_the_reviewers_repro_through_the_real_embed_stage(db_session, monkeypatch, exc):
    """As the review reproduced it: the real embed stage, its embedder call cancelling
    and re-enqueuing, and execute_job called without a claim (it reads one)."""
    from harbor_clerk.models import Chunk
    from harbor_clerk.worker import entry as entry_mod
    from harbor_clerk.worker.stages import embed as embed_stage

    doc_id = await _make_doc(db_session, JobStage.embed, name="repro")
    for i in range(10):
        db_session.add(Chunk(doc_id=doc_id, chunk_num=i, chunk_text=f"chunk {i}", language="english"))
    await db_session.commit()
    monkeypatch.setattr("harbor_clerk.worker.pipeline.advance_pipeline", lambda _d: None)

    def _embed(texts, **kw):
        _cancel_then_retry(doc_id, JobStage.embed)
        raise exc

    monkeypatch.setattr(embed_stage, "embed_texts", _embed)
    entry_mod.execute_job(doc_id, JobStage.embed, 0)

    job, doc = _read(doc_id, JobStage.embed)
    assert job.status == JobStatus.queued and not (job.metrics or {}).get("retry_after"), "re-enqueued row clobbered"
    assert doc.pipeline_status != PipelineStatus.error
