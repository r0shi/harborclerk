"""Tests for POST /api/system/reprocess-all-skip-summarize.

The endpoint re-enqueues the ingestion pipeline for every active document but
pre-inserts a "done-with-skipped" IngestionJob row for the summarize stage so
the worker cascade in `advance_pipeline` doesn't auto-enqueue it. Used for
fast iteration on non-summarize pipeline changes (metadata extractors,
chunker tweaks, embedder swaps).
"""

from sqlalchemy import select

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header


async def test_reprocess_all_skip_summarize_marks_summarize_as_done_with_skip_metric(
    client, admin_user, admin_token, db_session
):
    """The endpoint creates a 'done' IngestionJob row for summarize on every
    active doc, with metrics that record the skip + reason. The worker's
    cascade in advance_pipeline checks `if stage in existing_stages: continue`
    and won't enqueue summarize when a row already exists."""
    doc = Document(
        title="test-doc",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    db_session.add(doc)
    await db_session.commit()
    doc_id = doc.doc_id

    resp = await client.post(
        "/api/system/reprocess-all-skip-summarize",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reprocessed"] == 1
    assert body["skipped_stages"] == ["summarize"]

    # Doc was reset (pipeline_seq incremented; error cleared). pipeline_status
    # is `extracting` rather than `queued` because enqueue_stage(extract) flips
    # it to the stage's running_status immediately — same as production
    # reprocess_all behavior; the prior `ready` state is gone, which is what
    # matters for the reset semantics.
    await db_session.refresh(doc)
    assert doc.pipeline_seq == 2
    assert doc.error is None
    assert doc.pipeline_status != PipelineStatus.ready

    # Summarize job row exists, marked done with the skip metric
    jobs = (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id))).scalars().all()
    by_stage = {j.stage: j for j in jobs}

    assert JobStage.summarize in by_stage, "summarize skip-marker row should be inserted"
    summarize_job = by_stage[JobStage.summarize]
    assert summarize_job.status == JobStatus.done
    assert summarize_job.metrics.get("skipped") is True
    assert summarize_job.metrics.get("reason") == "reprocess_all_skip_summarize"

    # Extract job enqueued by the cascade-kickoff
    assert JobStage.extract in by_stage, "extract should be queued to start the cascade"
    assert by_stage[JobStage.extract].status == JobStatus.queued


async def test_reprocess_all_skip_summarize_handles_multiple_docs(client, admin_user, admin_token, db_session):
    """All active docs get the skip marker; status='inactive' docs are ignored."""
    active_a = Document(
        title="active-a",
        status="active",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    active_b = Document(
        title="active-b",
        status="active",
        sha256=b"\x02" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    inactive = Document(
        title="inactive",
        status="inactive",
        sha256=b"\x03" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    db_session.add_all([active_a, active_b, inactive])
    await db_session.commit()

    resp = await client.post(
        "/api/system/reprocess-all-skip-summarize",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["reprocessed"] == 2

    # Both active docs have the summarize skip-marker; inactive doc has no jobs
    for doc in (active_a, active_b):
        jobs = (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id))).scalars().all()
        stages = {j.stage for j in jobs}
        assert JobStage.summarize in stages
        assert JobStage.extract in stages

    inactive_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == inactive.doc_id))).scalars().all()
    )
    assert inactive_jobs == []


async def test_reprocess_all_skip_summarize_requires_admin(client, regular_user, user_token, db_session):
    """Non-admin callers get 403."""
    resp = await client.post(
        "/api/system/reprocess-all-skip-summarize",
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403


async def test_reprocess_all_skip_summarize_zero_docs(client, admin_user, admin_token, db_session):
    """No active docs → reprocessed=0, no error."""
    resp = await client.post(
        "/api/system/reprocess-all-skip-summarize",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"reprocessed": 0, "skipped_stages": ["summarize"]}
