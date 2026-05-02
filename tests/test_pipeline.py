"""Tests for the worker pipeline orchestrator + pipeline_seq race protection."""

import pytest

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import check_pipeline_seq
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
        from sqlalchemy import select

        refreshed_doc = sync_session.execute(select(Document).where(Document.doc_id == doc.doc_id)).scalar_one()
        assert refreshed_doc.pipeline_status == PipelineStatus.ready

        refreshed_job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        assert refreshed_job.status == JobStatus.done
    finally:
        sync_session.close()
