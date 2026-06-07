"""Tests for the worker pipeline orchestrator + pipeline_seq race protection."""

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running
from harbor_clerk.worker.stages.finalize import run_finalize


@pytest.mark.asyncio
async def test_check_pipeline_seq_passes_when_unchanged(db_session):
    """A worker that read seq=0 at start should see check_pipeline_seq return True
    if no content change has happened since."""
    doc = Document(
        title="Test unchanged",
        canonical_filename="test.pdf",
        status="active",
        sha256=b"x" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    assert doc.pipeline_seq == 0

    # check_pipeline_seq uses a sync session — create one against the same DB
    sync_session = get_sync_session()
    try:
        result = check_pipeline_seq(sync_session, doc.doc_id, 0)
    finally:
        sync_session.close()

    assert result is True


@pytest.mark.asyncio
async def test_check_pipeline_seq_fails_when_seq_bumped(db_session):
    """After a content change bumps pipeline_seq, an old worker's check returns False."""
    doc = Document(
        title="Test bumped",
        canonical_filename="bumped.pdf",
        status="active",
        sha256=b"y" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Worker reads seq=0 at start of a stage
    worker_seq = doc.pipeline_seq
    assert worker_seq == 0

    # A watcher modify event bumps the seq (simulates watcher's _reprocess_doc)
    doc.pipeline_seq = 1
    await db_session.commit()

    # The original worker's check returns False — it should abort writing results
    sync_session = get_sync_session()
    try:
        result = check_pipeline_seq(sync_session, doc.doc_id, worker_seq)
    finally:
        sync_session.close()

    assert result is False


@pytest.mark.asyncio
async def test_check_pipeline_seq_passes_at_higher_seq(db_session):
    """A worker that started with seq=2 sees True when seq is still 2."""
    doc = Document(
        title="Test higher seq",
        canonical_filename="higher.pdf",
        status="active",
        sha256=b"z" * 32,
        pipeline_status=PipelineStatus.queued,
        pipeline_seq=2,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    assert doc.pipeline_seq == 2

    sync_session = get_sync_session()
    try:
        # Worker that started with seq=2: matches → True
        assert check_pipeline_seq(sync_session, doc.doc_id, 2) is True
        # Old worker that started with seq=1: mismatch → False
        assert check_pipeline_seq(sync_session, doc.doc_id, 1) is False
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_run_finalize_completes_without_typeerror(db_session):
    """Regression: finalize.py was passing doc_id both positionally AND as a kwarg
    to mark_stage_done(), which raises `TypeError: got multiple values for argument 'doc_id'`.
    Asserting that run_finalize completes without exception covers the regression."""
    doc = Document(
        title="Finalize regression",
        canonical_filename="finalize.pdf",
        status="active",
        sha256=b"f" * 32,
        pipeline_status=PipelineStatus.summarized,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # The finalize stage requires a queued IngestionJob row to claim
    job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.queued)
    db_session.add(job)
    await db_session.commit()

    # run_finalize is sync; call it directly. If it raises TypeError, this test fails.
    run_finalize(doc.doc_id)

    # Verify side effects: pipeline_status flipped to ready, job marked done
    sync_session = get_sync_session()
    try:
        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed_doc.pipeline_status == PipelineStatus.ready

        refreshed_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert refreshed_job.status == JobStatus.done
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_mark_stage_done_skips_when_seq_bumped(db_session):
    """Race scenario: stage commits its data, then a watcher reprocess deletes
    the in-flight job and inserts a fresh queued one (bumping pipeline_seq).
    The original worker's mark_stage_done must NOT mark the new queued job
    as done — that would falsely advance the pipeline on stale content.
    """
    doc = Document(
        title="Race protection",
        canonical_filename="race.pdf",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.extracting,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Worker started with seq=0
    worker_seq = doc.pipeline_seq
    assert worker_seq == 0

    # Simulate reprocess: bump the seq and create a fresh queued job for the same stage.
    # (In production _reprocess_doc deletes the old job first; here we just create the
    # fresh one — the seq mismatch alone is enough to trigger the guard.)
    doc.pipeline_seq = 1
    fresh_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued, pipeline_seq=1)
    db_session.add(fresh_job)
    await db_session.commit()

    # Old worker calls mark_stage_done with its stale seq
    mark_stage_done(doc.doc_id, JobStage.extract, worker_seq=worker_seq)

    # The fresh queued job must NOT have been marked done
    sync_session = get_sync_session()
    try:
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.extract)
        ).scalar_one()
        assert job.status == JobStatus.queued, (
            "The fresh queued job was falsely marked done — pipeline_seq race protection failed"
        )
        # Doc pipeline_status must NOT have been advanced to extracted either
        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed_doc.pipeline_status == PipelineStatus.extracting
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_mark_stage_done_proceeds_when_seq_matches(db_session):
    """When pipeline_seq still matches, mark_stage_done marks done as expected."""
    doc = Document(
        title="Seq match",
        canonical_filename="match.pdf",
        status="active",
        sha256=b"m" * 32,
        pipeline_status=PipelineStatus.extracting,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    job = IngestionJob(
        doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.running, pipeline_seq=doc.pipeline_seq
    )
    db_session.add(job)
    await db_session.commit()

    mark_stage_done(doc.doc_id, JobStage.extract, worker_seq=doc.pipeline_seq)

    sync_session = get_sync_session()
    try:
        refreshed_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.extract)
        ).scalar_one()
        assert refreshed_job.status == JobStatus.done
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_mark_stage_done_silent_when_job_row_missing(db_session):
    """If the IngestionJob row was deleted (e.g. by reprocess) and we somehow
    pass the seq check anyway, mark_stage_done must exit silently rather than
    raising — the new pipeline owns its own job rows."""
    doc = Document(
        title="No job row",
        canonical_filename="nojob.pdf",
        status="active",
        sha256=b"n" * 32,
        pipeline_status=PipelineStatus.extracting,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # No IngestionJob row exists. mark_stage_done with matching seq should not raise.
    mark_stage_done(doc.doc_id, JobStage.extract, worker_seq=doc.pipeline_seq)

    # No exception raised — that's the test. Doc state is unchanged.
    sync_session = get_sync_session()
    try:
        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed_doc.pipeline_status == PipelineStatus.extracting
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_mark_stage_running_returns_false_when_seq_bumped(db_session):
    """mark_stage_running with worker_seq mismatch returns False so the worker
    skips the stage cleanly instead of running on stale content."""
    doc = Document(
        title="Seq bumped pre-run",
        canonical_filename="prerun.pdf",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.queued,
        pipeline_seq=2,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.running, pipeline_seq=2)
    db_session.add(job)
    await db_session.commit()

    # Worker thinks seq is 1 (it's actually 2) — should refuse to run
    assert mark_stage_running(doc.doc_id, JobStage.extract, worker_seq=1) is False
    # No worker_seq passed → resolves the current generation, succeeds.
    assert mark_stage_running(doc.doc_id, JobStage.extract) is True


@pytest.mark.asyncio
async def test_mark_stage_running_returns_false_when_job_row_missing(db_session):
    """If reprocess deleted the IngestionJob row before mark_stage_running runs,
    return False instead of raising NoResultFound."""
    doc = Document(
        title="Missing job row",
        canonical_filename="missing.pdf",
        status="active",
        sha256=b"q" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # No IngestionJob row — must not raise.
    assert mark_stage_running(doc.doc_id, JobStage.extract) is False


@pytest.mark.asyncio
async def test_run_finalize_aborts_when_seq_bumped_mid_stage(db_session):
    """End-to-end: a stage running with seq=0 sees seq bumped to 1 mid-flight.
    The stage's check_pipeline_seq guard fires before the data write AND
    mark_stage_done's guard fires after — neither updates the new pipeline's job.
    """
    doc = Document(
        title="Mid-stage race",
        canonical_filename="midstage.pdf",
        status="active",
        sha256=b"e" * 32,
        pipeline_status=PipelineStatus.summarized,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Old job that the worker is "processing"
    old_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.running, pipeline_seq=0)
    db_session.add(old_job)
    await db_session.commit()

    # Watcher reprocess fires: delete old job, bump seq, create fresh queued job.
    # Flush the delete before the insert to satisfy the (doc_id, stage) unique
    # constraint — mirrors the production sequence in _reprocess_doc.
    await db_session.delete(old_job)
    await db_session.flush()
    doc.pipeline_seq = 1
    fresh_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.queued, pipeline_seq=1)
    db_session.add(fresh_job)
    await db_session.commit()

    # Old worker now calls run_finalize with the generation it claimed. It must
    # not run to completion against the fresh job for pipeline_seq=1.
    run_finalize(doc.doc_id, worker_seq=0)

    sync_session = get_sync_session()
    try:
        refreshed_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert refreshed_job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_summarize_done_after_finalize_does_not_regress_status(db_session):
    """Background stages (summarize) must not regress pipeline_status from
    `ready` back to their own done-status. Doc reaches `ready` once finalize
    fires on the {entities, embed} gate; a later-completing summarize used
    to flip the user-visible badge from Ready → summarized."""
    doc = Document(
        title="Background regress check",
        canonical_filename="bg.pdf",
        status="active",
        sha256=b"b" * 32,
        needs_ocr=False,
        pipeline_status=PipelineStatus.ready,  # finalize already ran
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Realistic post-finalize state: every gate stage is done, summarize
    # lingers in queued because it's slow.
    sync_session = get_sync_session()
    try:
        for stage in (
            JobStage.extract,
            JobStage.chunk,
            JobStage.entities,
            JobStage.embed,
            JobStage.finalize,
        ):
            sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.done))
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                metrics={
                    "blocked": True,
                    "reason": "apple_intelligence_unavailable",
                    "retry_after": "2026-01-01T00:00:00+00:00",
                    "retry_attempts": 2,
                },
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    assert mark_stage_running(doc.doc_id, JobStage.summarize) is True
    mark_stage_done(doc.doc_id, JobStage.summarize)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        # KEY assertion: still ready, NOT regressed to summarized.
        assert refreshed.pipeline_status == PipelineStatus.ready
        # Job row marked done independently of pipeline_status.
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.done
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_successful_summarize_clears_afm_retry_metadata(db_session):
    """A recovered AFM retry should not leave done jobs looking blocked."""
    doc = Document(
        title="AFM recovered",
        canonical_filename="afm-recovered.pdf",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.running,
                pipeline_seq=doc.pipeline_seq,
                metrics={
                    "blocked": True,
                    "reason": "apple_intelligence_unavailable",
                    "retry_after": "2026-01-01T00:00:00+00:00",
                    "retry_attempts": 3,
                    "last_error": "AppleIntelligenceUnavailableError: daemon timed out",
                },
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    mark_stage_done(doc.doc_id, JobStage.summarize, worker_seq=doc.pipeline_seq)

    sync_session = get_sync_session()
    try:
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.done
        assert job.metrics == {}
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_summarize_not_in_finalize_gate(db_session):
    """advance_pipeline must enqueue finalize once {entities, embed} are done,
    even if summarize is still running. This is the core fan-in change —
    summarize is a background stage now."""
    from harbor_clerk.worker.pipeline import advance_pipeline

    doc = Document(
        title="Fan-in gate",
        canonical_filename="gate.pdf",
        status="active",
        sha256=b"g" * 32,
        pipeline_status=PipelineStatus.embedded,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        # Sequential prefix done
        for stage in (JobStage.extract, JobStage.chunk):
            sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.done))
        # Fan-in trio: entities + embed done, summarize still running
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.entities, status=JobStatus.done))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.done))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.running))
        sync_session.commit()
    finally:
        sync_session.close()

    advance_pipeline(doc.doc_id)

    # Finalize should now be queued — gate ignores in-flight summarize.
    sync_session = get_sync_session()
    try:
        finalize_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert finalize_job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_reprocess_regenerates_existing_summary_after_chunk(db_session, monkeypatch):
    """A full reprocess keeps the old summary text until summarize finishes,
    but the new pipeline generation must still enqueue summarize again.
    """
    from harbor_clerk.worker import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_is_ner_available", lambda: True)

    doc = Document(
        title="Existing summary reprocess",
        canonical_filename="old-summary.pdf",
        status="active",
        sha256=b"s" * 32,
        needs_ocr=False,
        pipeline_status=PipelineStatus.chunked,
        pipeline_seq=3,
        summary="Old summary that should be regenerated after full reprocess.",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.done, pipeline_seq=3))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.ocr, status=JobStatus.done, pipeline_seq=3))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.done, pipeline_seq=3))
        sync_session.commit()
    finally:
        sync_session.close()

    pipeline_mod.advance_pipeline(doc.doc_id)

    sync_session = get_sync_session()
    try:
        jobs = {
            job.stage: job
            for job in sync_session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id)).scalars()
        }
        summarize_job = jobs[JobStage.summarize]
        assert summarize_job.status == JobStatus.queued
        assert summarize_job.pipeline_seq == doc.pipeline_seq
        assert summarize_job.metrics == {}

        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed_doc.summary == "Old summary that should be regenerated after full reprocess."
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_advance_pipeline_does_not_re_enqueue_errored_background_stage(db_session):
    """Regression: previously advance_pipeline would re-enqueue an errored
    summarize on every tick because the check was 'not in (done, queued,
    running)'. Re-enqueue caused two bad effects: (1) infinite-ish retry of
    a broken summary, and (2) returning before the phase-3 finalize gate,
    so finalize never fired for any doc whose summarize errored. Bug
    visible in production as 10,576 docs stuck with summarize=error,
    finalize=missing, pipeline_status=error.

    Now: an errored background stage stays errored. Admin endpoints
    (/system/resummarize-all, /docs/X/resummarize) handle explicit retry.
    """
    from harbor_clerk.worker.pipeline import advance_pipeline

    doc = Document(
        title="Errored background no-retry",
        canonical_filename="err.pdf",
        status="active",
        sha256=b"e" * 32,
        pipeline_status=PipelineStatus.embedded,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        for stage in (JobStage.extract, JobStage.chunk, JobStage.entities, JobStage.embed):
            sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.done))
        # Summarize errored on its first attempt.
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.error,
                error="AttributeError: type object 'ChatMessage' has no attribute 'id'",
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    advance_pipeline(doc.doc_id)

    sync_session = get_sync_session()
    try:
        # Summarize stays errored — NOT auto-retried back to queued.
        summarize_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert summarize_job.status == JobStatus.error

        # Finalize fires — gate is {entities, embed}, both done.
        finalize_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert finalize_job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_background_stage_error_does_not_poison_pipeline_status(db_session, monkeypatch):
    """Regression: the worker error handler in entry.py used to set
    doc.pipeline_status = error on any stage failure, including background
    stages. PR #327 designed background stages NOT to touch pipeline_status
    (both enqueue_stage and mark_stage_done guard against it) but the error
    path was missed. Effect: a doc fully ingested through finalize would
    flip from Ready → error when its (optional) summary errored later.
    """
    from harbor_clerk.worker.entry import execute_job

    doc = Document(
        title="Background error no-poison",
        canonical_filename="poison.pdf",
        status="active",
        sha256=b"p" * 32,
        # Doc has already finalized — pipeline_status should stay here.
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued))
        sync_session.commit()
    finally:
        sync_session.close()

    # Make the summarize stage raise so we exercise the error branch.
    def _boom(_doc_id, *, worker_seq=None):
        raise AttributeError("type object 'ChatMessage' has no attribute 'id'")

    from harbor_clerk.worker import entry as entry_mod

    monkeypatch.setitem(entry_mod.STAGE_FUNCTIONS, JobStage.summarize, _boom)

    execute_job(doc.doc_id, JobStage.summarize)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        # KEY assertion: pipeline_status stays ready, NOT regressed to error.
        assert refreshed.pipeline_status == PipelineStatus.ready
        assert refreshed.error is None

        # The job itself records the error (so the UI's summary-state chip
        # can show "Failed") — that's separate from doc.pipeline_status.
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.error
        assert "ChatMessage" in (job.error or "")
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_forced_afm_unavailable_requeues_summarize_with_retry(db_session, monkeypatch):
    """Forced Apple Intelligence failures should pause summary generation
    instead of converting every queued summary into a document-level failure."""
    from harbor_clerk.llm.summarize import AppleIntelligenceUnavailableError
    from harbor_clerk.worker.entry import execute_job
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc = Document(
        title="AFM unavailable",
        canonical_filename="afm.pdf",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="A substantial document body."))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued))
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *args, **kwargs: None)

    def _afm_unavailable(*args, **kwargs):
        raise AppleIntelligenceUnavailableError(
            "Apple Intelligence summaries are enabled but unavailable: daemon timed out"
        )

    monkeypatch.setattr(summarize_stage, "generate_summary", _afm_unavailable)

    execute_job(doc.doc_id, JobStage.summarize)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed.pipeline_status == PipelineStatus.ready
        assert refreshed.summary is None

        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.queued
        assert job.error is None
        assert job.started_at is None
        assert job.heartbeat_at is None
        assert job.metrics["blocked"] is True
        assert job.metrics["reason"] == "apple_intelligence_unavailable"
        assert job.metrics["retry_attempts"] == 1
        assert "AppleIntelligenceUnavailableError" in job.metrics["last_error"]
        assert "daemon timed out" in job.metrics["last_error"]
        assert job.metrics["retry_after"]
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_forced_afm_content_rejection_errors_only_that_summary(db_session, monkeypatch):
    """A per-document Apple safety refusal should not pause the whole summary queue."""
    from harbor_clerk.llm.summarize import AppleIntelligenceContentRejectedError
    from harbor_clerk.worker.entry import execute_job
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc = Document(
        title="AFM rejected",
        canonical_filename="afm-rejected.pdf",
        status="active",
        sha256=b"j" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="A substantial document body."))
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                metrics={
                    "blocked": True,
                    "reason": "apple_intelligence_unavailable",
                    "retry_after": "2026-01-01T00:00:00+00:00",
                    "retry_attempts": 2,
                },
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *args, **kwargs: None)

    def _afm_rejected(*args, **kwargs):
        raise AppleIntelligenceContentRejectedError(
            "Apple Intelligence refused to summarize this document: Detected content likely to be unsafe"
        )

    monkeypatch.setattr(summarize_stage, "generate_summary", _afm_rejected)

    execute_job(doc.doc_id, JobStage.summarize)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed.pipeline_status == PipelineStatus.ready
        assert refreshed.summary is None

        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.error
        assert "AppleIntelligenceContentRejectedError" in (job.error or "")
        assert job.metrics == {}
        assert job.finished_at is not None
    finally:
        sync_session.close()


def test_afm_retry_delay_sequence_starts_fast():
    from harbor_clerk.worker import entry as entry_mod

    delays = [entry_mod._afm_retry_delay_seconds(attempt) for attempt in range(1, 9)]
    assert delays == [1, 5, 10, 30, 60, 120, 300, 600]


@pytest.mark.asyncio
async def test_forced_afm_unavailable_marks_single_job_error_after_retry_budget(db_session, monkeypatch):
    """The AFM retry loop eventually declares one job futile without
    stampeding through the whole summary backlog."""
    from harbor_clerk.llm.summarize import AppleIntelligenceUnavailableError
    from harbor_clerk.worker import entry as entry_mod
    from harbor_clerk.worker.stages import summarize as summarize_stage

    doc = Document(
        title="AFM futile",
        canonical_filename="afm-futile.pdf",
        status="active",
        sha256=b"f" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="A substantial document body."))
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                metrics={
                    "blocked": True,
                    "reason": "apple_intelligence_unavailable",
                    "retry_attempts": entry_mod.AFM_RETRY_MAX_ATTEMPTS - 1,
                    "retry_after": "2026-01-01T00:00:00+00:00",
                },
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(summarize_stage, "_wait_for_idle_llm", lambda *args, **kwargs: None)

    def _afm_unavailable(*args, **kwargs):
        raise AppleIntelligenceUnavailableError(
            "Apple Intelligence summaries are enabled but unavailable: daemon timed out"
        )

    monkeypatch.setattr(summarize_stage, "generate_summary", _afm_unavailable)

    entry_mod.execute_job(doc.doc_id, JobStage.summarize)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed.pipeline_status == PipelineStatus.ready

        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.error
        assert "AppleIntelligenceUnavailableError" in (job.error or "")
        assert job.metrics["retry_attempts"] == entry_mod.AFM_RETRY_MAX_ATTEMPTS
        assert job.metrics["retry_exhausted"] is True
        assert job.metrics["blocked"] is False
        assert "retry_after" not in job.metrics
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_claim_next_job_skips_summarize_during_afm_cooldown(db_session, monkeypatch):
    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="AFM cooldown",
        canonical_filename="cooldown.pdf",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    sync_session = get_sync_session()
    try:
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued))
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(entry_mod, "refresh_llm_settings", lambda: None)
    monkeypatch.setattr(entry_mod, "get_settings", lambda: SimpleNamespace(summary_force_apple_intelligence=True))
    monkeypatch.setattr("harbor_clerk.llm.summarize.apple_intelligence_cooldown", lambda: (300.0, "daemon timed out"))

    assert entry_mod.claim_next_job([JobStage.summarize]) is None

    sync_session = get_sync_session()
    try:
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        assert job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_claim_next_job_prefers_due_afm_retry_probe(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from harbor_clerk.worker import entry as entry_mod

    normal_doc = Document(
        title="Normal summary",
        canonical_filename="normal.pdf",
        status="active",
        sha256=b"n" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    retry_doc = Document(
        title="Due AFM retry",
        canonical_filename="due-retry.pdf",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add_all([normal_doc, retry_doc])
    await db_session.commit()
    await db_session.refresh(normal_doc)
    await db_session.refresh(retry_doc)

    retry_after = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    sync_session = get_sync_session()
    try:
        sync_session.add_all(
            [
                IngestionJob(doc_id=normal_doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued),
                IngestionJob(
                    doc_id=retry_doc.doc_id,
                    stage=JobStage.summarize,
                    status=JobStatus.queued,
                    metrics={"reason": "apple_intelligence_unavailable", "retry_after": retry_after},
                ),
            ]
        )
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(entry_mod, "refresh_llm_settings", lambda: None)
    monkeypatch.setattr(entry_mod, "get_settings", lambda: SimpleNamespace(summary_force_apple_intelligence=True))
    monkeypatch.setattr("harbor_clerk.llm.summarize.apple_intelligence_cooldown", lambda: None)

    result = entry_mod.claim_next_job([JobStage.summarize])
    assert result is not None
    claimed_doc_id, claimed_stage, _ = result
    assert claimed_doc_id == retry_doc.doc_id
    assert claimed_stage == JobStage.summarize

    sync_session = get_sync_session()
    try:
        retry_job = sync_session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == retry_doc.doc_id, IngestionJob.stage == JobStage.summarize
            )
        ).scalar_one()
        normal_job = sync_session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == normal_doc.doc_id,
                IngestionJob.stage == JobStage.summarize,
            )
        ).scalar_one()
        assert retry_job.status == JobStatus.running
        assert normal_job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_claim_next_job_skips_summarize_until_retry_after(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="AFM retry later",
        canonical_filename="retry.pdf",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    fresh_doc = Document(
        title="Fresh summary should wait",
        canonical_filename="fresh.pdf",
        status="active",
        sha256=b"w" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add_all([doc, fresh_doc])
    await db_session.commit()
    await db_session.refresh(doc)
    await db_session.refresh(fresh_doc)

    retry_after = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    sync_session = get_sync_session()
    try:
        sync_session.add_all(
            [
                IngestionJob(
                    doc_id=doc.doc_id,
                    stage=JobStage.summarize,
                    status=JobStatus.queued,
                    metrics={"reason": "apple_intelligence_unavailable", "retry_after": retry_after},
                ),
                IngestionJob(doc_id=fresh_doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued),
            ]
        )
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(entry_mod, "refresh_llm_settings", lambda: None)
    monkeypatch.setattr(entry_mod, "get_settings", lambda: SimpleNamespace(summary_force_apple_intelligence=True))
    monkeypatch.setattr("harbor_clerk.llm.summarize.apple_intelligence_cooldown", lambda: None)

    assert entry_mod.claim_next_job([JobStage.summarize]) is None

    sync_session = get_sync_session()
    try:
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.summarize)
        ).scalar_one()
        fresh_job = sync_session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == fresh_doc.doc_id,
                IngestionJob.stage == JobStage.summarize,
            )
        ).scalar_one()
        assert job.status == JobStatus.queued
        assert fresh_job.status == JobStatus.queued
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_afm_idle_wait_wakes_for_next_retry(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from harbor_clerk.worker import entry as entry_mod

    doc = Document(
        title="Retry wakeup",
        canonical_filename="retry-wakeup.pdf",
        status="active",
        sha256=b"u" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    retry_after = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    sync_session = get_sync_session()
    try:
        sync_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                metrics={"reason": "apple_intelligence_unavailable", "retry_after": retry_after},
            )
        )
        sync_session.commit()
    finally:
        sync_session.close()

    monkeypatch.setattr(entry_mod, "refresh_llm_settings", lambda: None)
    monkeypatch.setattr(entry_mod, "get_settings", lambda: SimpleNamespace(summary_force_apple_intelligence=True))
    monkeypatch.setattr("harbor_clerk.llm.summarize.apple_intelligence_cooldown", lambda: None)

    wait_seconds = entry_mod._next_afm_retry_wait_seconds([JobStage.summarize], default=30)
    assert 1 <= wait_seconds <= 2


@pytest.mark.asyncio
async def test_run_chunk_markdown_aligns_with_headings_and_preserves_code_fence(db_session, tmp_path):
    """Integration: a Markdown doc with multiple headings and a code fence
    produces chunks whose boundaries prefer heading lines and never split
    inside the fence."""
    from sqlalchemy import select

    from harbor_clerk.models.chunk import Chunk
    from harbor_clerk.models.document_heading import DocumentHeading
    from harbor_clerk.worker.stages.chunk import run_chunk
    from harbor_clerk.worker.stages.extract import run_extract

    # Body chosen so it produces multiple chunks AND includes a code fence
    # the chunker must not split. Pad each section so the chunker hits the
    # default target (1000) inside each section.
    pad_a = "A " * 250
    pad_b = "B " * 250
    pad_c = "C " * 250
    md = (
        "# Section A\n\n"
        + pad_a
        + "\n\n# Section B\n\n"
        + pad_b
        + "\n\n```python\n"
        + ("print('keep me intact')\n" * 30)
        + "```\n\n"
        + "# Section C\n\n"
        + pad_c
        + "\n"
    )
    md_path = tmp_path / "note.md"
    md_path.write_text(md)

    doc = Document(
        title=md_path.stem,
        canonical_filename=md_path.name,
        status="active",
        sha256=hashlib.sha256(md_path.read_bytes()).digest(),
        source_path=str(md_path),
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.chunk, status=JobStatus.queued))
    await db_session.commit()

    run_extract(doc.doc_id)
    run_chunk(doc.doc_id)

    sync_session = get_sync_session()
    try:
        headings = (
            sync_session.execute(
                select(DocumentHeading).where(DocumentHeading.doc_id == doc.doc_id).order_by(DocumentHeading.position)
            )
            .scalars()
            .all()
        )
        chunks = (
            sync_session.execute(select(Chunk).where(Chunk.doc_id == doc.doc_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )
    finally:
        sync_session.close()

    assert len(headings) == 3, f"expected 3 headings, got {len(headings)}"
    assert {h.title for h in headings} == {"Section A", "Section B", "Section C"}
    assert len(chunks) >= 2, "expected the doc to produce multiple chunks"

    # The Python code fence must not be split at its boundary: at least one chunk
    # must contain the complete fence (both the opening ``` and closing ```) in a
    # single chunk_text. Overlap windows may cause later chunks to pick up the
    # closing ``` line, but the primary chunk that spans the fence must be intact.
    fence_marker = "print('keep me intact')"
    chunks_with_fence = [c for c in chunks if fence_marker in c.chunk_text]
    assert chunks_with_fence, "fence content was not retained in any chunk"
    complete_fence_chunks = [c for c in chunks_with_fence if c.chunk_text.count("```") >= 2]
    assert complete_fence_chunks, (
        "no chunk contains a complete code fence (both opening and closing ```); "
        f"fence chunks and their backtick counts: "
        f"{[(c.chunk_num, c.chunk_text.count('```')) for c in chunks_with_fence]}"
    )

    # Heading alignment: at least one chunk should start at a heading boundary.
    heading_titles = {"Section A", "Section B", "Section C"}
    aligned = 0
    for c in chunks:
        head = c.chunk_text.lstrip()[:30]
        if any(head.startswith(title) for title in heading_titles):
            aligned += 1
    assert aligned >= 1, (
        "expected at least one chunk to start at a heading boundary; "
        f"chunk heads: {[c.chunk_text[:30] for c in chunks]!r}"
    )


@pytest.mark.asyncio
async def test_run_extract_markdown_writes_headings_and_overrides_title(db_session, tmp_path):
    """Integration test: run_extract on a .md file with frontmatter writes
    document_headings rows AND updates doc.title from the frontmatter."""
    from harbor_clerk.models.document_heading import DocumentHeading
    from harbor_clerk.worker.stages.extract import run_extract

    # Write a markdown file with frontmatter + two headings.
    md_path = tmp_path / "note.md"
    md_path.write_text(
        "---\ntitle: Real Title\ntags: [x, y]\n---\n# Section A\n\nProse.\n\n## Section B\n\nMore prose.\n"
    )

    doc = Document(
        title=md_path.stem,
        canonical_filename=md_path.name,
        status="active",
        sha256=hashlib.sha256(md_path.read_bytes()).digest(),
        source_path=str(md_path),
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.queued,
        )
    )
    await db_session.commit()

    # run_extract is sync; call it directly.
    run_extract(doc.doc_id)

    # Re-fetch and verify via a fresh sync session (run_extract commits its own session).
    sync_session = get_sync_session()
    try:
        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        headings = (
            sync_session.execute(
                select(DocumentHeading).where(DocumentHeading.doc_id == doc.doc_id).order_by(DocumentHeading.position)
            )
            .scalars()
            .all()
        )

        assert refreshed_doc.title == "Real Title"  # frontmatter override applied
        assert len(headings) == 2
        titles = [h.title for h in headings]
        assert "Section A" in titles
        assert "Section B" in titles
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_run_extract_image_renamed_as_office_persists_image_mime(db_session, tmp_path):
    """A JPEG saved with a .docx extension must reroute to the image/OCR path
    AND have doc.mime_type corrected to image/jpeg. The ocr stage dispatches on
    doc.mime_type (not extract's local routing), so without the persisted
    correction the doc would be flagged needs_ocr here but then dropped by
    ocr's mime dispatch — empty page, no searchable text."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract as extract_mod
    from harbor_clerk.worker.stages.extract import run_extract

    # Minimal JPEG header (SOI + APP0/JFIF). Extract only sniffs the leading
    # bytes — it sets an empty page for images and ocr does the real work — so
    # a valid signature is all this needs.
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    path = tmp_path / "report.docx"
    path.write_bytes(jpeg_bytes)

    doc = Document(
        title=path.stem,
        canonical_filename=path.name,
        status="active",
        sha256=hashlib.sha256(jpeg_bytes).digest(),
        source_path=str(path),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()

    with patch.object(extract_mod, "run_metadata_extractors", return_value={}):
        run_extract(doc.doc_id)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed.mime_type == "image/jpeg"  # corrected so ocr will dispatch to the image branch
        assert refreshed.needs_ocr is True
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_run_extract_office_renamed_as_pdf_routes_to_tika_autodetect(db_session, tmp_path):
    """An .xlsx workbook saved with a .pdf extension declares application/pdf
    but its bytes are a ZIP container. Extract must NOT send it down the PDF
    path (PDFBox 422s on ZIP bytes); it reroutes to Tika with
    application/octet-stream so Tika auto-detects the Office subtype from the
    full stream."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract as extract_mod
    from harbor_clerk.worker.stages.extract import run_extract

    # ZIP local-file header — the prefix every modern Office / ODF / EPUB
    # container starts with.
    zip_bytes = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 64
    path = tmp_path / "budget.pdf"
    path.write_bytes(zip_bytes)

    doc = Document(
        title=path.stem,
        canonical_filename=path.name,
        status="active",
        sha256=hashlib.sha256(zip_bytes).digest(),
        source_path=str(path),
        mime_type="application/pdf",
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()

    with (
        patch.object(extract_mod, "_extract_via_tika", return_value=[(1, "Sheet1 revenue 100")]) as mock_tika,
        patch.object(extract_mod, "_extract_headings_via_tika", return_value=[]),
        patch.object(extract_mod, "run_metadata_extractors", return_value={}),
    ):
        run_extract(doc.doc_id)

    # Tika was asked to auto-detect (octet-stream), NOT to parse the bytes as PDF.
    assert mock_tika.called
    assert mock_tika.call_args.args[1] == "application/octet-stream"

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed.needs_ocr is False  # neither image nor PDF → ocr correctly skipped
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_wikilink_graph_resolves_and_kb_find_related_returns_linked(db_session, _engine, tmp_path, monkeypatch):
    """Integration: doc A and doc B link to each other via [[…]]. After
    extract + finalize, the document_links rows resolve in both directions.
    kb_find_related on either doc returns the other with source='linked'."""
    import json
    import uuid
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.mcp_server import _mcp_principal, kb_find_related
    from harbor_clerk.models.document_link import DocumentLink
    from harbor_clerk.worker.stages.extract import run_extract

    # Patch `async_session_factory` to use the test session's engine (which is
    # already bound to this test's event loop via the session-scoped fixture).
    # We deliberately do NOT bind to db_session's connection — this test commits
    # mid-flight to make rows visible to the sync extract/finalize stages, and
    # AsyncSession.commit() under NullPool releases the underlying connection,
    # so a captured-up-front connection would be closed by the time the MCP
    # call runs. Going through the engine gives us a fresh connection each
    # invocation, matching the production behaviour.
    test_factory = async_sessionmaker(_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with test_factory() as session:
            yield session

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)

    async def _ingest(md_text: str, name: str) -> uuid.UUID:
        md_path = tmp_path / name
        md_path.write_text(md_text)
        doc = Document(
            title=md_path.stem,
            canonical_filename=md_path.name,
            status="active",
            sha256=hashlib.sha256(md_path.read_bytes()).digest(),
            source_path=str(md_path),
            pipeline_status=PipelineStatus.queued,
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
        db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.queued))
        await db_session.commit()
        return doc.doc_id

    # Doc A links to "Note B"; Doc B links to "Note A".
    a_id = await _ingest("# Note A\n\nSee [[Note B]] for details.\n", "Note A.md")
    b_id = await _ingest("# Note B\n\nBack to [[Note A]].\n", "Note B.md")

    # Extract both.
    run_extract(a_id)
    run_extract(b_id)
    # Finalize in order — A first, then B. After B finalizes the dangling
    # link from A to B should be resolved by the dangling-resolve pass.
    run_finalize(a_id)
    run_finalize(b_id)

    # Re-read links via a fresh sync session (stages committed on their own).
    sync_session = get_sync_session()
    try:
        links = sync_session.execute(select(DocumentLink).order_by(DocumentLink.src_doc_id)).scalars().all()
        assert len(links) == 2, (
            f"expected 2 links total, got {len(links)}: "
            f"{[(link.src_doc_id, link.target_title, link.resolved) for link in links]}"
        )
        # Both must be resolved after finalize finishes for both docs.
        assert all(link.resolved for link in links), (
            f"links unresolved: {[(link.target_title, link.resolved) for link in links]}"
        )
        # A→B and B→A both present.
        pairs = {(link.src_doc_id, link.target_doc_id) for link in links}
        assert (a_id, b_id) in pairs
        assert (b_id, a_id) in pairs
    finally:
        sync_session.close()

    # kb_find_related on A returns B as a 'linked' result. The MCP function is
    # registered via @mcp.tool() but FastMCP returns the underlying function
    # unchanged, so we await it directly. A principal must be set first because
    # _get_principal() raises PermissionError without one.
    principal_token = _mcp_principal.set(Principal(type="user", id=uuid.uuid4(), role="admin"))
    try:
        result_str = await kb_find_related(doc_id=str(a_id), k=5)
    finally:
        _mcp_principal.reset(principal_token)

    result = json.loads(result_str)
    related = result.get("related", [])
    by_id = {r["doc_id"]: r for r in related}
    assert str(b_id) in by_id, f"B not in kb_find_related(A): {related!r}"
    assert by_id[str(b_id)]["source"] == "linked"
    assert by_id[str(b_id)]["similarity"] == 1.0
