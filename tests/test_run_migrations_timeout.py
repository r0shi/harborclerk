"""POST /api/system/run-migrations cancels the migration's backend when it times out (#825).

``subprocess.run(timeout=...)`` kills alembic, but its backend keeps running the statement it was
sent: 0009's table rewrite would hold ACCESS EXCLUSIVE on chunks to its end, blocking every search,
and then roll back anyway. The route names alembic's session (``application_name``, set by
alembic/env.py from ``HC_MIGRATION_APPLICATION_NAME``), cancels every backend carrying the name
(parallel workers included), counts the client sessions, waits for them to exit, and reports the
revision the database is at: a run that started below 0008 keeps 0008's committed work but not its
stamp, so "rolled back" would be wrong. Every wait here is bounded.
"""

import os
import subprocess
import threading
import time

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import harbor_clerk.api.routes.system as system_routes
import harbor_clerk.migration_session as migration_session
from harbor_clerk.migration_session import APPLICATION_NAME_ENV
from tests.conftest import auth_header

_SYNC_DSN = os.environ["DATABASE_URL"].replace("+asyncpg", "")


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
    """In a subprocess: env.py calls asyncio.run, which cannot nest in this test's event loop."""
    subprocess.run(["alembic", *args], check=True, env=dict(os.environ))


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


async def test_timeout_cancels_every_backend_and_counts_the_session(client, admin_token, monkeypatch):
    """Stub alembic: a real client backend carrying the run's name sits in a 30 s statement run by a
    parallel worker (which carries the leader's name), as alembic's does after its client is
    killed. Both are cancelled; the message counts one session."""
    state: dict = {}

    def _sleeper(name: str) -> None:
        conn = psycopg2.connect(_SYNC_DSN, application_name=name)
        conn.autocommit = True
        started = time.monotonic()
        try:
            with conn.cursor() as cur:
                cur.execute("SET debug_parallel_query = on")
                cur.execute("SELECT pg_sleep(30)")
            state["outcome"] = "finished"
        except psycopg2.errors.QueryCanceled:
            state["outcome"] = "cancelled"
        finally:
            state["after"] = time.monotonic() - started
            conn.close()

    def _fake_run(args, **kwargs):
        name = kwargs["env"][APPLICATION_NAME_ENV]
        state["name"] = name
        state["thread"] = threading.Thread(target=_sleeper, args=(name,), daemon=True)
        state["thread"].start()
        _wait_for(
            lambda: sorted(t for t, _ in _backends(name)) == ["client backend", "parallel worker"],
            "the named client backend and its parallel worker",
        )
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _fake_run)
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        if "thread" in state:
            state["thread"].join(35)
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert "(1 database session(s) stopped)" in detail, detail
    assert f"the database is at revision {_version()}." in detail, detail
    assert "rolled back" not in detail
    assert state["name"].startswith("harbor-clerk-alembic-")
    assert state["outcome"] == "cancelled" and state["after"] < 15, state
    assert _backends(state["name"]) == []


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
    """End to end: real alembic (env.py names its session) from ``start``, with 0009's ALTER TABLE
    held on a lock on chunks; at the deadline the route kills it, cancels its backend by name, and
    reports the revision the database is actually at."""
    # The admin user is flushed in an open transaction; a CREATE INDEX CONCURRENTLY (0008, either
    # direction) waits for every transaction holding an xid, so this test's own would hang it.
    await db_session.commit()
    _alembic("upgrade", "head")
    _alembic("downgrade", start)
    monkeypatch.setattr(system_routes, "MIGRATION_TIMEOUT_SECONDS", 8)
    blocker = psycopg2.connect(_SYNC_DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("SET lock_timeout = '60s'")
            cur.execute("LOCK TABLE public.chunks IN ACCESS SHARE MODE")
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
        assert resp.status_code == 504, resp.text
        detail = resp.json()["detail"]
        assert "(1 database session(s) stopped)" in detail, detail
        assert f"the database is at revision {left_at}." in detail, detail
        (name,) = run_name
        _wait_for(lambda: _backends(name) == [], "the cancelled migration backend to exit")
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
    assert _version() == "0009_fts_over_folded_text"
