"""Orphaned ``running`` jobs go back to the queue, from the admin route and the background reaper alike (#864).

``POST /api/system/reaper-run`` used to call ``enqueue_stage`` on each orphan,
which skips a row already ``running`` at the current pipeline_seq, so it
reported ``reaped: N`` while every orphan stayed ``running``. Both reapers now
share ``worker/pipeline.py``'s selection (the status summary's stuck count),
its conditional write and its announcement.
"""

import asyncio
import json
import select as select_module
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header

_STALE = timedelta(minutes=10)


async def _doc_with_job(
    db_session, sha: bytes, *, stage: JobStage, doc_status: str = "active", job_seq: int = 3, **job_fields
) -> uuid.UUID:
    """A document at pipeline_seq 3 with one ``running`` job row; returns the job's id."""
    doc = Document(
        title=f"doc-{sha[:1].decode()}",
        status=doc_status,
        sha256=sha * 32,
        pipeline_status=PipelineStatus.embedding,
        pipeline_seq=3,
    )
    db_session.add(doc)
    await db_session.flush()
    job = IngestionJob(doc_id=doc.doc_id, stage=stage, status=JobStatus.running, pipeline_seq=job_seq, **job_fields)
    db_session.add(job)
    await db_session.flush()
    return job.job_id


async def _reload(db_session, job_id: uuid.UUID) -> IngestionJob:
    db_session.expire_all()
    return (await db_session.execute(select(IngestionJob).where(IngestionJob.job_id == job_id))).scalar_one()


async def _reap(client, admin_token) -> int:
    resp = await client.post("/api/system/reaper-run", headers=auth_header(admin_token))
    assert resp.status_code == 200
    return resp.json()["reaped"]


async def test_reaper_run_requeues_stale_orphans_and_leaves_live_runs(client, admin_user, admin_token, db_session):
    now = datetime.now(UTC)
    stale = await _doc_with_job(
        db_session,
        b"s",
        stage=JobStage.embed,
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - _STALE,
        error="stale text",
        progress_current=4,
        progress_total=9,
    )
    # No heartbeat, started more than twice the stage timeout ago.
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

    assert await _reap(client, admin_token) == 2

    for orphan in (stale, legacy):
        row = await _reload(db_session, orphan)
        assert row.status == JobStatus.queued
        assert row.pipeline_seq == 3
        assert row.started_at is None
        assert row.heartbeat_at is None
        assert row.error is None
        assert (row.progress_current, row.progress_total) == (0, 0)

    row = await _reload(db_session, live)
    assert row.status == JobStatus.running
    assert row.heartbeat_at is not None
    assert row.started_at is not None

    row = await _reload(db_session, legacy_recent)
    assert row.status == JobStatus.running
    assert row.started_at is not None


async def test_reaper_run_with_nothing_stale_reports_zero(client, admin_user, admin_token, db_session):
    now = datetime.now(UTC)
    live = await _doc_with_job(
        db_session, b"f", stage=JobStage.embed, started_at=now - timedelta(minutes=2), heartbeat_at=now
    )

    assert await _reap(client, admin_token) == 0
    assert (await _reload(db_session, live)).status == JobStatus.running


async def test_reaper_run_leaves_a_job_claimed_after_it_was_read(
    client, admin_user, admin_token, db_session, monkeypatch
):
    """Another reaper requeues the orphan and a worker claims it between the route's read
    and its write: the route must leave the live claim alone and not count it."""
    from harbor_clerk.worker import entry, pipeline

    now = datetime.now(UTC)
    job_id = await _doc_with_job(
        db_session, b"s", stage=JobStage.embed, started_at=now - timedelta(hours=1), heartbeat_at=now - _STALE
    )
    await db_session.commit()

    real = pipeline.requeue_orphan_job
    claims = []

    async def _requeued_and_claimed_meanwhile(session, job):
        other = get_sync_session()
        try:
            other.execute(
                update(IngestionJob)
                .where(IngestionJob.job_id == job_id)
                .values(status=JobStatus.queued, started_at=None, heartbeat_at=None)
            )
            other.commit()
        finally:
            other.close()
        claims.append(entry.claim_next_job([JobStage.embed]))
        return await real(session, job)

    monkeypatch.setattr(pipeline, "requeue_orphan_job", _requeued_and_claimed_meanwhile)

    assert await _reap(client, admin_token) == 0
    assert claims[0] is not None

    row = await _reload(db_session, job_id)
    assert row.status == JobStatus.running
    assert row.started_at is not None and row.started_at > now
    assert row.heartbeat_at is not None
    # No second worker can claim the (doc, stage, seq) the first is running.
    assert entry.claim_next_job([JobStage.embed]) is None


def _write_meanwhile(monkeypatch, job_id: uuid.UUID, **values) -> None:
    """Between a reaper's read of the orphan and its write, another session commits ``values`` to the row."""
    from harbor_clerk.worker import pipeline

    real = pipeline.requeue_orphan_job

    async def _changed_meanwhile(session, job):
        other = get_sync_session()
        try:
            other.execute(update(IngestionJob).where(IngestionJob.job_id == job_id).values(**values))
            other.commit()
        finally:
            other.close()
        return await real(session, job)

    monkeypatch.setattr(pipeline, "requeue_orphan_job", _changed_meanwhile)


def _record_announcements(monkeypatch) -> list[list[tuple[uuid.UUID, JobStage]]]:
    from harbor_clerk.worker import pipeline

    calls: list[list[tuple[uuid.UUID, JobStage]]] = []
    real = pipeline.announce_requeued_jobs

    def _announce(requeued):
        calls.append(list(requeued))
        real(requeued)

    monkeypatch.setattr(pipeline, "announce_requeued_jobs", _announce)
    return calls


async def test_reaper_run_leaves_a_job_whose_worker_heartbeated_after_it_was_read(
    client, admin_user, admin_token, db_session, monkeypatch
):
    """Same claim (started_at), but the worker heartbeated late, between the read and the write:
    it is alive, so the row is not requeued or counted."""
    now = datetime.now(UTC)
    started = now - timedelta(hours=1)
    job_id = await _doc_with_job(db_session, b"h", stage=JobStage.embed, started_at=started, heartbeat_at=now - _STALE)
    await db_session.commit()
    fresh = datetime.now(UTC)
    _write_meanwhile(monkeypatch, job_id, heartbeat_at=fresh)

    assert await _reap(client, admin_token) == 0

    row = await _reload(db_session, job_id)
    assert row.status == JobStatus.running
    assert row.started_at == started
    assert row.heartbeat_at == fresh


async def test_a_legacy_orphan_requeued_by_another_reaper_is_not_counted_again(
    client, admin_user, admin_token, db_session, monkeypatch
):
    """No heartbeat and no started_at, so only the status tells the second write that the
    first reaper already requeued it: it must not count or announce it again."""
    now = datetime.now(UTC)
    job_id = await _doc_with_job(
        db_session,
        b"g",
        stage=JobStage.extract,
        created_at=now - timedelta(hours=3),
        started_at=None,
        heartbeat_at=None,
    )
    await db_session.commit()
    _write_meanwhile(monkeypatch, job_id, status=JobStatus.queued, progress_current=0, progress_total=0)
    announcements = _record_announcements(monkeypatch)

    assert await _reap(client, admin_token) == 0

    assert all(not requeued for requeued in announcements)
    assert (await _reload(db_session, job_id)).status == JobStatus.queued


async def test_orphans_of_inactive_documents_or_old_generations_are_neither_stuck_nor_reaped(
    client, admin_user, admin_token, db_session, monkeypatch, _engine
):
    now = datetime.now(UTC)
    stale = dict(started_at=now - timedelta(hours=1), heartbeat_at=now - _STALE)
    on_deleted_doc = await _doc_with_job(db_session, b"d", stage=JobStage.embed, doc_status="deleted", **stale)
    old_generation = await _doc_with_job(db_session, b"o", stage=JobStage.embed, job_seq=2, **stale)
    await db_session.commit()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.json()["counts"]["stuck_jobs"] == 0
    assert await _reap(client, admin_token) == 0
    assert await _run_background_reaper_once(monkeypatch, _engine) == []

    for job_id in (on_deleted_doc, old_generation):
        assert (await _reload(db_session, job_id)).status == JobStatus.running


async def test_reaper_run_announces_each_requeued_job_after_committing_it(
    client, admin_user, admin_token, db_session, monkeypatch
):
    """A pg_notify on the stage's queue and a "queued" job event, as enqueue_stage sends, and
    only once the requeue is committed: a worker woken by the notify claims the job."""
    from harbor_clerk.worker import entry, pipeline

    now = datetime.now(UTC)
    job_id = await _doc_with_job(
        db_session, b"n", stage=JobStage.embed, started_at=now - timedelta(hours=1), heartbeat_at=now - _STALE
    )
    doc_id = (await _reload(db_session, job_id)).doc_id
    await db_session.commit()

    conn = entry._get_listen_connection()
    got: list[tuple[str, str]] = []
    claims = []

    def _drain(until, seconds: float = 5) -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=seconds)
        while not until() and datetime.now(UTC) < deadline:
            select_module.select([conn], [], [], 0.2)
            conn.poll()
            got.extend((n.channel, n.payload) for n in conn.notifies)
            conn.notifies.clear()

    real_announce = pipeline.announce_requeued_jobs

    def _announce_then_wake_a_worker(requeued):
        real_announce(requeued)
        # Still inside the route: the worker the notify wakes claims the job now.
        _drain(lambda: any(channel == "job_enqueued_cpu" for channel, _ in got))
        claims.append(entry.claim_next_job([JobStage.embed]))

    monkeypatch.setattr(pipeline, "announce_requeued_jobs", _announce_then_wake_a_worker)
    try:
        with conn.cursor() as cur:
            cur.execute("LISTEN job_enqueued_cpu")
            cur.execute("LISTEN job_progress")
        assert await _reap(client, admin_token) == 1
        _drain(lambda: any(channel == "job_progress" for channel, _ in got))
    finally:
        conn.close()

    # (doc, stage, seq) first: #862 appends the claim's started_at to what claim_next_job returns.
    assert len(claims) == 1 and claims[0] is not None
    assert tuple(claims[0][:3]) == (doc_id, JobStage.embed, 3)
    assert ("job_enqueued_cpu", str(doc_id)) in got
    events = [json.loads(payload) for channel, payload in got if channel == "job_progress"]
    assert len(events) == 1
    assert events[0]["doc_id"] == str(doc_id)
    assert events[0]["stage"] == "embed"
    assert events[0]["status"] == "queued"


async def _run_background_reaper_once(
    monkeypatch, engine, *, announce_error: Exception | None = None
) -> list[tuple[uuid.UUID, JobStage]]:
    """One pass of api/app.py's ``_session_reaper_loop``, against the test database.

    Returns what it announced (``announce_requeued_jobs`` still runs, or raises
    ``announce_error``). Asserts the pass ran to its end, the topic refresh."""
    from harbor_clerk.api import app as app_module
    from harbor_clerk.worker import pipeline

    announced: list[tuple[uuid.UUID, JobStage]] = []
    real_announce = pipeline.announce_requeued_jobs

    def _announce(requeued):
        announced.extend(requeued)
        if announce_error is not None:
            raise announce_error
        real_announce(requeued)

    monkeypatch.setattr(pipeline, "announce_requeued_jobs", _announce)
    sleeps = 0
    topic_refreshes = 0

    async def _sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    async def _no_topics(_db):
        nonlocal topic_refreshes
        topic_refreshes += 1

    monkeypatch.setattr(app_module, "asyncio", types.SimpleNamespace(sleep=_sleep, to_thread=asyncio.to_thread))
    monkeypatch.setattr(
        "harbor_clerk.db.async_session_factory",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    monkeypatch.setattr("harbor_clerk.topics.check_and_recompute_topics", _no_topics)
    with pytest.raises(asyncio.CancelledError):
        await app_module._session_reaper_loop()
    assert sleeps == 2
    assert topic_refreshes == 1
    return announced


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
    assert await _reap(client, admin_token) == 1
    by_route = await _reload(db_session, by_route)
    route_state = {name: getattr(by_route, name) for name in _COMPARED}

    by_background = await _doc_with_job(db_session, b"b", stage=JobStage.embed, **fields)
    await db_session.commit()
    announced = await _run_background_reaper_once(monkeypatch, _engine)
    by_background = await _reload(db_session, by_background)
    assert announced == [(by_background.doc_id, JobStage.embed)]

    assert route_state["status"] == JobStatus.queued
    assert (route_state["progress_current"], route_state["progress_total"]) == (0, 0)
    assert route_state == {name: getattr(by_background, name) for name in _COMPARED}


async def test_a_job_shown_as_stuck_is_recovered_by_both_reapers(
    client, admin_user, admin_token, db_session, monkeypatch, _engine
):
    """The status summary's stuck count and both reapers share one definition, including
    the rows it reaches only through its fallbacks: no heartbeat, no started_at, an old created_at."""
    now = datetime.now(UTC)
    fields = dict(created_at=now - timedelta(hours=3), started_at=None, heartbeat_at=None)
    for_route = await _doc_with_job(db_session, b"a", stage=JobStage.extract, **fields)
    for_background = await _doc_with_job(db_session, b"b", stage=JobStage.extract, **fields)
    await db_session.commit()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["counts"]["stuck_jobs"] == 2

    # The route reaps every orphan; set one aside so the background reaper has its own.
    await db_session.execute(
        update(IngestionJob).where(IngestionJob.job_id == for_background).values(status=JobStatus.done)
    )
    await db_session.commit()
    assert await _reap(client, admin_token) == 1
    assert (await _reload(db_session, for_route)).status == JobStatus.queued

    await db_session.execute(
        update(IngestionJob).where(IngestionJob.job_id == for_background).values(status=JobStatus.running)
    )
    await db_session.commit()
    await _run_background_reaper_once(monkeypatch, _engine)
    assert (await _reload(db_session, for_background)).status == JobStatus.queued

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.json()["counts"]["stuck_jobs"] == 0


async def test_a_failed_announcement_does_not_fail_the_route(client, admin_user, admin_token, db_session, monkeypatch):
    """The requeue is committed before the announcement: if announcing fails, the route logs
    it and still returns the count, and the job stays queued for a worker's idle poll."""
    from harbor_clerk.worker import pipeline

    def _broken(_requeued):
        raise RuntimeError("notify failed")

    monkeypatch.setattr(pipeline, "announce_requeued_jobs", _broken)
    now = datetime.now(UTC)
    job_id = await _doc_with_job(
        db_session, b"x", stage=JobStage.embed, started_at=now - timedelta(hours=1), heartbeat_at=now - _STALE
    )

    assert await _reap(client, admin_token) == 1
    assert (await _reload(db_session, job_id)).status == JobStatus.queued


async def test_a_failed_announcement_does_not_cut_the_background_pass_short(db_session, monkeypatch, _engine):
    """The background reaper logs a failed announcement and goes on to the purges and the
    topic refresh (asserted by the helper); the requeue stays committed."""
    now = datetime.now(UTC)
    job_id = await _doc_with_job(
        db_session, b"y", stage=JobStage.embed, started_at=now - timedelta(hours=1), heartbeat_at=now - _STALE
    )
    await db_session.commit()

    announced = await _run_background_reaper_once(monkeypatch, _engine, announce_error=RuntimeError("notify failed"))

    row = await _reload(db_session, job_id)
    assert announced == [(row.doc_id, JobStage.embed)]
    assert row.status == JobStatus.queued
