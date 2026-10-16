"""POST /api/system/run-migrations: its alembic child and that child's backend never outlive the run (#825).

Killing alembic does not stop its backend: a statement already sent (0009's table rewrite) runs to
its end under ACCESS EXCLUSIVE and only then rolls back. The route names alembic's session
(``application_name``, set by alembic/env.py from ``HC_MIGRATION_APPLICATION_NAME``) and, on a
timeout or any other abnormal exit (the request cancelled, an error), kills alembic and cancels
every backend carrying the name (parallel workers included), waits for them to exit, and reports
what it could establish: the client sessions stopped, and the revision the database is at (a run
that started below 0008 keeps 0008's committed work but not its stamp; a run past its commit is
complete). Every wait here is bounded.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import harbor_clerk.api.routes.system as system_routes
import harbor_clerk.migration_session as migration_session
from harbor_clerk.api.deps import Principal
from harbor_clerk.models.audit_log import AuditLog
from tests.conftest import auth_header

_SYNC_DSN = os.environ["DATABASE_URL"].replace("+asyncpg", "")
HEAD = "0009_fts_over_folded_text"


@pytest.fixture(autouse=True)
async def _session_factory_on_this_loop(monkeypatch):
    """The route opens its own sessions from harbor_clerk.db; its pooled engine would hand this
    test's event loop a connection made on an earlier test's loop."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    monkeypatch.setattr("harbor_clerk.db.async_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    yield
    await engine.dispose()


@pytest.fixture
def run_name(monkeypatch):
    """The exact application_name the route gives its alembic run."""
    names: list[str] = []
    original = migration_session.unique_application_name

    def _recording():
        names.append(original())
        return names[-1]

    monkeypatch.setattr(migration_session, "unique_application_name", _recording)
    return names


def _alembic(*args: str) -> None:
    """In a subprocess (env.py calls asyncio.run, which cannot nest in this test's event loop), bounded."""
    subprocess.run(["alembic", *args], check=True, env=dict(os.environ), timeout=300)


def _backends(name: str) -> list[tuple[str, str | None]]:
    """(backend_type, wait_event_type) of every backend in this database carrying ``name``."""
    conn = psycopg2.connect(_SYNC_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT backend_type, wait_event_type FROM pg_stat_activity "
                "WHERE application_name = %s AND datname = current_database() AND pid <> pg_backend_pid()",
                (name,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _wait_for(predicate, what: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _version() -> str:
    conn = psycopg2.connect(_SYNC_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            return cur.fetchone()[0]
    finally:
        conn.close()


class _Sleeper:
    """A real client backend named ``name``, in a 30 s statement (through a parallel worker when
    ``parallel``), as alembic's backend is after its client is gone."""

    def __init__(self, name: str, *, parallel: bool = False):
        self.outcome: str | None = None
        self.after: float | None = None
        self._thread = threading.Thread(target=self._run, args=(name, parallel), daemon=True)
        self._thread.start()

    def _run(self, name: str, parallel: bool) -> None:
        conn = psycopg2.connect(_SYNC_DSN, application_name=name)
        conn.autocommit = True
        started = time.monotonic()
        try:
            with conn.cursor() as cur:
                if parallel:
                    cur.execute("SET debug_parallel_query = on")
                cur.execute("SELECT pg_sleep(30)")
            self.outcome = "finished"
        except psycopg2.errors.QueryCanceled:
            self.outcome = "cancelled"
        finally:
            self.after = time.monotonic() - started
            conn.close()

    def join(self) -> None:
        self._thread.join(35)


class _Pipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeAlembic:
    """Stands in for the alembic child: ``communicate`` runs ``on_communicate`` (which may raise
    TimeoutExpired), then returns ``returncode``/``stderr``, or with ``hang`` blocks until killed."""

    def __init__(self, name: str, *, returncode: int = 0, stderr: str = "", on_communicate=None, hang=False):
        self.name = name
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode: int | None = None
        self.killed = False
        self.entered = threading.Event()
        self._rc = returncode
        self._stderr = stderr
        self._on_communicate = on_communicate
        self._hang = hang
        self._dead = threading.Event()

    def communicate(self, timeout=None):
        self.entered.set()
        if self._on_communicate is not None:
            self._on_communicate(self, timeout)
        if self._hang:
            self._dead.wait(60)
            return "", ""
        self.returncode = self._rc
        return "", self._stderr

    def kill(self):
        self.killed = True
        self._dead.set()

    def wait(self):
        self._dead.wait(60)
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


def _fake(monkeypatch, **kwargs) -> list[_FakeAlembic]:
    started: list[_FakeAlembic] = []

    def _start(session_name):
        started.append(_FakeAlembic(session_name, **kwargs))
        return started[-1]

    monkeypatch.setattr(system_routes, "_start_alembic", _start)
    return started


def _times_out(proc, timeout):
    raise subprocess.TimeoutExpired(["alembic"], timeout)


async def _audits(db_session) -> list[dict]:
    """The run_migrations audit details, oldest first."""
    rows = await db_session.execute(
        select(AuditLog.detail).where(AuditLog.action == "run_migrations").order_by(AuditLog.created_at)
    )
    return [r[0] for r in rows]


def _hold_migration_lock(application_name: str):
    """A session holding the migration lock, as a run in progress does."""
    conn = psycopg2.connect(_SYNC_DSN, application_name=application_name)
    conn.autocommit = True
    with conn.cursor() as cur:
        # Never the blocking pg_advisory_lock in a test: a leftover holder would hang it.
        cur.execute("SELECT pg_try_advisory_lock(%s)", (migration_session.MIGRATION_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        conn.close()
        raise AssertionError("the migration lock is already held: a leftover run or holder")
    return conn


# ---------------------------------------------------------------------------
# Normal exits
# ---------------------------------------------------------------------------


async def test_success_returns_the_schema_version_and_audits(client, admin_token, admin_user, db_session, monkeypatch):
    admin_id = admin_user.user_id  # the route's version read rolls the session back, expiring the object
    started = _fake(monkeypatch, returncode=0)
    resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "schema_version": _version()}
    assert started[0].name.startswith("harbor-clerk-alembic-") and not started[0].killed
    rows = (
        await db_session.execute(select(AuditLog.user_id, AuditLog.detail).where(AuditLog.action == "run_migrations"))
    ).all()
    assert rows == [(admin_id, {"outcome": "ok", "version": _version()})]


async def test_alembic_failure_is_a_500_with_its_stderr(client, admin_token, db_session, monkeypatch):
    started = _fake(monkeypatch, returncode=1, stderr="Traceback ... boom")
    resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]
    assert not started[0].killed
    assert await _audits(db_session) == [{"outcome": "error", "version": _version()}]


async def test_a_run_in_progress_is_a_409_not_a_second_run(client, admin_token, db_session, monkeypatch):
    """Another run holds the migration lock (a second tab, a remounted page, the Mac app's runner):
    refused before alembic starts, rather than queued behind it."""
    await db_session.commit()  # the 409 path reads the version, which rolls the request session back
    started = _fake(monkeypatch)
    holder = _hold_migration_lock("hc-test-other-run")
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        holder.close()
    assert resp.status_code == 409
    assert "another migration run is in progress" in resp.json()["detail"].lower()
    assert started == []
    assert await _audits(db_session) == [{"outcome": "busy", "version": _version()}]


async def test_non_admin_is_refused(client, user_token, monkeypatch):
    started = _fake(monkeypatch)
    resp = await client.post("/api/system/run-migrations", headers=auth_header(user_token))
    assert resp.status_code == 403
    assert started == []


# ---------------------------------------------------------------------------
# Timeouts (stubbed alembic, real backends)
# ---------------------------------------------------------------------------


async def test_timeout_cancels_every_backend_and_waits_for_them(client, admin_token, monkeypatch):
    """A client backend and its parallel worker carry the run's name. Both are cancelled, the
    message counts one session, and they are gone by the time the response arrives."""
    sleepers: list[_Sleeper] = []

    def _start_sleeper_then_time_out(proc, timeout):
        sleepers.append(_Sleeper(proc.name, parallel=True))
        _wait_for(
            lambda: sorted(t for t, _ in _backends(proc.name)) == ["client backend", "parallel worker"],
            "the named client backend and its parallel worker",
        )
        _times_out(proc, timeout)

    started = _fake(monkeypatch, on_communicate=_start_sleeper_then_time_out)
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
        assert _backends(started[0].name) == [], "the response arrived before the cancelled backends exited"
    finally:
        for sleeper in sleepers:
            sleeper.join()
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "(1 database session(s) stopped)" in detail, detail
    assert f"revision {_version()}" in detail, detail
    assert "rolled back" not in detail and "still stopping" not in detail
    assert started[0].killed
    assert started[0].stdout.closed and started[0].stderr.closed
    assert sleepers[0].outcome == "cancelled" and sleepers[0].after < 15


async def test_the_cancel_does_not_wait_on_an_exhausted_api_pool(client, admin_token, monkeypatch):
    """At the deadline, requests queued behind the rewrite hold every pooled connection. The cancel
    uses a connection of its own: with the API pool's only connection held (and a 2 s pool
    timeout) the backend is still cancelled and the stop confirmed."""
    exhausted = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=2)
    monkeypatch.setattr("harbor_clerk.db.engine", exhausted)
    monkeypatch.setattr("harbor_clerk.db.async_session_factory", async_sessionmaker(exhausted, expire_on_commit=False))
    sleepers: list[_Sleeper] = []

    def _start_sleeper_then_time_out(proc, timeout):
        sleepers.append(_Sleeper(proc.name))
        _wait_for(lambda: len(_backends(proc.name)) == 1, "the named backend")
        _times_out(proc, timeout)

    _fake(monkeypatch, on_communicate=_start_sleeper_then_time_out)
    held = await exhausted.connect()  # the pool's only connection
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        await held.close()
        await exhausted.dispose()
        for sleeper in sleepers:
            sleeper.join()
    assert resp.status_code == 504
    assert "(1 database session(s) stopped)" in resp.json()["detail"], resp.json()["detail"]
    assert sleepers[0].outcome == "cancelled"


async def test_a_run_that_only_waited_for_another_says_so(client, admin_token, db_session, monkeypatch):
    """The narrow race past the pre-check: another run takes the lock just after it, and this run
    spends the deadline polling (an idle session carrying its name). The 504 says another run is in
    progress and this one changed nothing."""
    conns: list = []

    def _queued_then_time_out(proc, timeout):
        conns.append(_hold_migration_lock("hc-test-other-run"))
        waiter = psycopg2.connect(_SYNC_DSN, application_name=proc.name)
        waiter.autocommit = True
        conns.append(waiter)
        _times_out(proc, timeout)

    _fake(monkeypatch, on_communicate=_queued_then_time_out)
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        for conn in conns:
            conn.close()
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "waiting for another migration run" in detail and "changed nothing" in detail, detail
    assert await _audits(db_session) == [{"outcome": "busy", "version": _version()}]


async def test_timeout_reports_a_backend_that_does_not_stop(client, admin_token, monkeypatch):
    """An idle session carrying the name: a cancel has nothing to interrupt, so it stays, and the
    message says the run is still stopping instead of claiming it stopped."""
    monkeypatch.setattr(system_routes, "MIGRATION_CANCEL_WAIT_SECONDS", 0.5)
    idle: list = []

    def _idle_then_time_out(proc, timeout):
        conn = psycopg2.connect(_SYNC_DSN, application_name=proc.name)
        conn.autocommit = True
        idle.append(conn)
        _times_out(proc, timeout)

    _fake(monkeypatch, on_communicate=_idle_then_time_out)
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        for conn in idle:
            conn.close()
    assert resp.status_code == 504
    assert "(1 database session(s) stopped (still stopping))" in resp.json()["detail"]


async def test_timeout_after_commit_says_the_migration_completed(client, admin_token, db_session, monkeypatch):
    """The deadline fell after the run committed: the database is at the script head."""
    _fake(monkeypatch, on_communicate=_times_out)
    assert _version() == HEAD
    resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "the migration completed" in detail and f"revision {HEAD}" in detail, detail
    assert "Restart the app" not in detail
    assert await _audits(db_session) == [{"outcome": "timeout", "version": HEAD}]


async def test_timeout_still_answers_when_the_cancel_fails(client, admin_token, db_session, monkeypatch):
    async def _broken(*args, **kwargs):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(migration_session, "cancel_backends", _broken)
    started = _fake(monkeypatch, on_communicate=_times_out)
    resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "could not be confirmed" in detail and "database unreachable" in detail, detail
    assert started[0].killed
    assert await _audits(db_session) == [{"outcome": "timeout", "version": "unknown"}]


# ---------------------------------------------------------------------------
# The request cancelled mid-run (shutdown, client gone)
# ---------------------------------------------------------------------------


async def test_a_cancelled_request_kills_alembic_and_cancels_its_backend(db_session, admin_user, monkeypatch):
    started = _fake(monkeypatch, hang=True)
    task = asyncio.create_task(
        system_routes.run_migrations(
            admin=Principal(type="user", id=admin_user.user_id, role="admin"), session=db_session
        )
    )
    sleeper = None
    try:
        await asyncio.to_thread(_wait_for, lambda: bool(started) and started[0].entered.is_set(), "alembic to start")
        sleeper = _Sleeper(started[0].name)
        await asyncio.to_thread(_wait_for, lambda: len(_backends(started[0].name)) == 1, "the named backend")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
        assert started[0].killed
        assert started[0].stdout.closed and started[0].stderr.closed
        assert _backends(started[0].name) == []
    finally:
        if sleeper is not None:
            await asyncio.to_thread(sleeper.join)
    assert sleeper.outcome == "cancelled"
    assert await _audits(db_session) == [{"outcome": "cancelled", "version": _version()}]


async def test_alembic_that_cannot_start_is_audited_as_an_error(db_session, admin_user, monkeypatch):
    await db_session.commit()

    def _no_python(session_name):
        raise FileNotFoundError("no interpreter")

    monkeypatch.setattr(system_routes, "_start_alembic", _no_python)
    with pytest.raises(FileNotFoundError):
        await system_routes.run_migrations(
            admin=Principal(type="user", id=admin_user.user_id, role="admin"), session=db_session
        )
    assert await _audits(db_session) == [{"outcome": "error", "version": None}]


async def test_a_failing_pre_check_is_audited_as_an_error(db_session, admin_user, monkeypatch):
    await db_session.commit()

    async def _broken(session):
        raise RuntimeError("pg_locks unreadable")

    monkeypatch.setattr(migration_session, "migration_lock_holder", _broken)
    started = _fake(monkeypatch)
    with pytest.raises(RuntimeError, match="pg_locks unreadable"):
        await system_routes.run_migrations(
            admin=Principal(type="user", id=admin_user.user_id, role="admin"), session=db_session
        )
    assert started == []
    assert await _audits(db_session) == [{"outcome": "error", "version": None}]


def test_alembic_runs_under_this_interpreter(monkeypatch):
    calls: list = []

    def _record(args, **kwargs):
        calls.append(args)
        return object()

    monkeypatch.setattr(subprocess, "Popen", _record)
    system_routes._start_alembic("hc-test-name")
    assert calls == [[sys.executable, "-m", "alembic", "upgrade", "head"]]


# ---------------------------------------------------------------------------
# _schema_version
# ---------------------------------------------------------------------------


async def test_schema_version_unknown_without_a_row(db_session):
    await db_session.execute(text("DELETE FROM alembic_version"))  # rolled back by _schema_version
    assert await system_routes._schema_version(db_session) == "unknown"
    assert _version() == HEAD


async def test_schema_version_unknown_while_alembic_version_is_locked(db_session, monkeypatch):
    monkeypatch.setattr(system_routes, "SCHEMA_VERSION_LOCK_TIMEOUT", "200ms")
    blocker = psycopg2.connect(_SYNC_DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
        assert await system_routes._schema_version(db_session) == "unknown"
    finally:
        blocker.rollback()
        blocker.close()
    assert await system_routes._schema_version(db_session) == HEAD


# ---------------------------------------------------------------------------
# End to end: real alembic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "left_at"),
    [
        # 0009's rewrite is waiting on the lock when it is cancelled: nothing of 0009 lands.
        ("0008_chunk_text_ws_fold", "0008_chunk_text_ws_fold"),
        # 0008's autocommit block commits its function and index, but its stamp is in the
        # transaction 0009 runs in: the database stays at 0007 (and the retry accepts 0008's work).
        ("0007_ingestion_job_pipeline_seq", "0007_ingestion_job_pipeline_seq"),
    ],
)
async def test_timeout_reports_the_revision_a_real_run_leaves(
    db_session, client, admin_token, monkeypatch, run_name, start, left_at
):
    """Real alembic (env.py names its session) from ``start``, with 0009's ALTER TABLE held on a
    lock on chunks; at the deadline the route kills it, cancels its backend by name, and reports
    the revision the database is actually at."""
    # The admin user is flushed in an open transaction; a CREATE INDEX CONCURRENTLY (0008, either
    # direction) waits for every transaction holding a snapshot, so this test's own would hang it.
    await db_session.commit()
    _alembic("upgrade", "head")
    _alembic("downgrade", start)
    monkeypatch.setattr(system_routes, "MIGRATION_TIMEOUT_SECONDS", 8)
    blocker = psycopg2.connect(_SYNC_DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("SET lock_timeout = '60s'")
            cur.execute("LOCK TABLE public.chunks IN ACCESS SHARE MODE")
        try:
            resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
        finally:
            # The request authenticated on this session; if the route regressed and left its
            # transaction open, the upgrade in the finally below would wait on it forever.
            await db_session.rollback()
        assert resp.status_code == 504, resp.text
        detail = resp.json()["detail"]
        assert "(1 database session(s) stopped)" in detail, detail
        assert f"the database is at revision {left_at}." in detail, detail
        (name,) = run_name
        assert _backends(name) == [], "the response arrived before the cancelled backend exited"
        assert _version() == left_at
        # Either way the run got as far as 0009: 0008's function and index are committed.
        conn = psycopg2.connect(_SYNC_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regprocedure('public.hc_fold_whitespace(text)') IS NOT NULL, "
                    "(SELECT indisvalid FROM pg_index WHERE indexrelid = "
                    "to_regclass('public.ix_chunks_chunk_text_ws_trgm'))"
                )
                assert cur.fetchone() == (True, True)
        finally:
            conn.close()
    finally:
        blocker.rollback()
        blocker.close()
        _alembic("upgrade", "head")
    assert _version() == HEAD


def test_uuid_names_are_unique():
    """Two runs never share a name, so one run's cancel never reaches another's backend."""
    assert len({migration_session.unique_application_name() for _ in range(50)}) == 50
