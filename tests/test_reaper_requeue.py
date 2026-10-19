"""Orphaned ``running`` jobs go back to the queue, from the admin route and the background reaper alike (#864).

``POST /api/system/reaper-run`` used to call ``enqueue_stage`` on each orphan,
which skips a row already ``running`` at the current pipeline_seq, so it
reported ``reaped: N`` while every orphan stayed ``running``.
"""

import asyncio
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header

_STALE = timedelta(minutes=10)


async def _doc_with_job(db_session, sha: bytes, *, stage: JobStage, **job_fields) -> uuid.UUID:
    """A document at pipeline_seq 3 with one ``running`` job row; returns the job's id."""
    doc = Document(
        title=f"doc-{sha[:1].decode()}",
        status="active",
        sha256=sha * 32,
        pipeline_status=PipelineStatus.embedding,
        pipeline_seq=3,
    )
    db_session.add(doc)
    await db_session.flush()
    job = IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.running, pipeline_seq=3, **job_fields)
    db_session.add(job)
    await db_session.flush()
    return job.job_id


async def _reload(db_session, job_id: uuid.UUID) -> IngestionJob:
    db_session.expire_all()
    return (await db_session.execute(select(IngestionJob).where(IngestionJob.job_id == job_id))).scalar_one()


async def test_reaper_run_requeues_stale_orphans_and_leaves_live_runs(
    client, admin_user, admin_token, db_session, monkeypatch
):
    notified: list[tuple[str, int]] = []

    async def _record_notify(_session, queue_name, count):
        notified.append((queue_name, count))

    monkeypatch.setattr("harbor_clerk.api.routes.system._notify_bulk_queue", _record_notify)

    now = datetime.now(UTC)
    stale = await _doc_with_job(
        db_session,
        b"s",
        stage=JobStage.embed,
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - _STALE,
        error="stale text",
    )
    # No heartbeat, started more than twice the stage timeout ago: the route's legacy orphan rule.
    legacy = await _doc_with_job(
        db_session,
        b"l",
        stage=JobStage.extract,
        started_at=now - timedelta(hours=3),
        heartbeat_at=None,
    )
    live = await _doc_with_job(
        db_session,
        b"f",
        stage=JobStage.embed,
        started_at=now - timedelta(minutes=30),
        heartbeat_at=now - timedelta(seconds=5),
    )
    legacy_recent = await _doc_with_job(
        db_session,
        b"r",
        stage=JobStage.chunk,
        started_at=now - timedelta(minutes=1),
        heartbeat_at=None,
    )

    resp = await client.post("/api/system/reaper-run", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"reaped": 2}

    for orphan in (stale, legacy):
        row = await _reload(db_session, orphan)
        assert row.status == JobStatus.queued
        assert row.pipeline_seq == 3
        assert row.started_at is None
        assert row.heartbeat_at is None
        assert row.error is None

    row = await _reload(db_session, live)
    assert row.status == JobStatus.running
    assert row.heartbeat_at is not None
    assert row.started_at is not None

    row = await _reload(db_session, legacy_recent)
    assert row.status == JobStatus.running
    assert row.started_at is not None

    assert sorted(notified) == [("cpu", 1), ("io", 1)]


async def test_reaper_run_with_nothing_stale_reports_zero(client, admin_user, admin_token, db_session):
    now = datetime.now(UTC)
    live = await _doc_with_job(
        db_session, b"f", stage=JobStage.embed, started_at=now - timedelta(minutes=2), heartbeat_at=now
    )

    resp = await client.post("/api/system/reaper-run", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"reaped": 0}
    assert (await _reload(db_session, live)).status == JobStatus.running


async def _run_background_reaper_once(monkeypatch, engine) -> None:
    """One pass of api/app.py's ``_session_reaper_loop``, against the test database."""
    from harbor_clerk.api import app as app_module

    sleeps = 0

    async def _sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    async def _no_topics(_db):
        return None

    monkeypatch.setattr(app_module, "asyncio", types.SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(
        "harbor_clerk.db.async_session_factory",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    monkeypatch.setattr("harbor_clerk.topics.check_and_recompute_topics", _no_topics)
    with pytest.raises(asyncio.CancelledError):
        await app_module._session_reaper_loop()
    assert sleeps == 2


_COMPARED = (
    "status",
    "pipeline_seq",
    "started_at",
    "heartbeat_at",
    "finished_at",
    "error",
    "metrics",
    "progress_current",
    "progress_total",
    "priority",
    "created_at",
)


async def test_route_and_background_reaper_requeue_an_orphan_identically(
    client, admin_user, admin_token, db_session, monkeypatch, _engine
):
    now = datetime.now(UTC)
    created = now - timedelta(hours=2)
    fields = dict(
        created_at=created,
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - _STALE,
        progress_current=4,
        progress_total=9,
        priority=1,
        error="worker died",
        # An embed row carrying a deferred-retry schedule: whatever a requeue does
        # to it, both reapers must do the same.
        metrics={
            "reason": "embedder_unavailable",
            "retry_after": (now - timedelta(minutes=1)).isoformat(),
            "retry_attempts": 2,
        },
    )

    by_route = await _doc_with_job(db_session, b"a", stage=JobStage.embed, **fields)
    resp = await client.post("/api/system/reaper-run", headers=auth_header(admin_token))
    assert resp.json() == {"reaped": 1}
    by_route = await _reload(db_session, by_route)
    route_state = {name: getattr(by_route, name) for name in _COMPARED}

    by_background = await _doc_with_job(db_session, b"b", stage=JobStage.embed, **fields)
    await db_session.commit()
    await _run_background_reaper_once(monkeypatch, _engine)
    by_background = await _reload(db_session, by_background)

    assert route_state["status"] == JobStatus.queued
    assert route_state == {name: getattr(by_background, name) for name in _COMPARED}
