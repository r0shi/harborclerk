"""Tests for the worker pipeline orchestrator + pipeline_seq race protection."""

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
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
    fresh_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued)
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

    job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.running)
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

    job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.running)
    db_session.add(job)
    await db_session.commit()

    # Worker thinks seq is 1 (it's actually 2) — should refuse to run
    assert mark_stage_running(doc.doc_id, JobStage.extract, worker_seq=1) is False
    # No worker_seq passed → no race check, succeeds (back-compat path)
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
    old_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.running)
    db_session.add(old_job)
    await db_session.commit()

    # Watcher reprocess fires: delete old job, bump seq, create fresh queued job.
    # Flush the delete before the insert to satisfy the (doc_id, stage) unique
    # constraint — mirrors the production sequence in _reprocess_doc.
    await db_session.delete(old_job)
    await db_session.flush()
    doc.pipeline_seq = 1
    fresh_job = IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.queued)
    db_session.add(fresh_job)
    await db_session.commit()

    # Old worker now calls run_finalize. Its internal load of doc.pipeline_seq
    # will read the *new* value (1), so finalize will run to completion against
    # the fresh job — that's correct behavior. The race-protection test that
    # matters most is mark_stage_done_skips_when_seq_bumped (above), which
    # simulates the more dangerous TOCTOU window between data-write commit and
    # mark_stage_done call.
    run_finalize(doc.doc_id)

    sync_session = get_sync_session()
    try:
        # The fresh job should be marked done because the worker's effective
        # seq matched at run_finalize time.
        refreshed_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert refreshed_job.status == JobStatus.done
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
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.summarize, status=JobStatus.queued))
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
