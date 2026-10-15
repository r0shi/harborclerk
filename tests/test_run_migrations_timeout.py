"""POST /api/system/run-migrations cancels the migration's backend when it times out (#825).

``subprocess.run(timeout=...)`` kills alembic, but its backend keeps running the statement it was
sent: 0009's table rewrite would hold ACCESS EXCLUSIVE on chunks to its end, blocking every search,
and then roll back anyway. The route names alembic's session (``application_name``, set by
alembic/env.py from ``HC_MIGRATION_APPLICATION_NAME``) and cancels every backend carrying the name.
Every wait here is bounded.
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


def _backends_named(name: str, *, waiting_on_lock: bool = False) -> int:
    with psycopg2.connect(_SYNC_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s AND state = 'active' "
            "AND (NOT %s OR wait_event_type = 'Lock')",
            (name, waiting_on_lock),
        )
        count = cur.fetchone()[0]
    conn.close()
    return count


def _wait_for(predicate, what: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


async def test_timeout_cancels_the_backend_named_for_the_run(client, admin_token, monkeypatch):
    """Stub alembic: a real backend carrying the run's name sits in a 30 s statement, as alembic's
    does after its client is killed; the route must cancel it, not leave it running."""
    state: dict = {}

    def _sleeper(name: str) -> None:
        conn = psycopg2.connect(_SYNC_DSN, application_name=name)
        conn.autocommit = True
        started = time.monotonic()
        try:
            with conn.cursor() as cur:
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
        _wait_for(lambda: _backends_named(name) == 1, "the named backend to start")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _fake_run)
    try:
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
    finally:
        if "thread" in state:
            state["thread"].join(35)
    assert resp.status_code == 504
    assert "1 database session(s) stopped" in resp.json()["detail"]
    assert state["name"].startswith("harbor-clerk-alembic-")
    assert state["outcome"] == "cancelled" and state["after"] < 15, state


async def test_timeout_cancels_a_real_alembic_run_blocked_on_a_lock(client, admin_token, monkeypatch):
    """End to end: real alembic (env.py names its session), held on a lock on alembic_version so
    it cannot finish; at the deadline the route kills it and cancels its backend by name."""
    monkeypatch.setattr(system_routes, "MIGRATION_TIMEOUT_SECONDS", 4)
    blocker = psycopg2.connect(_SYNC_DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("SET lock_timeout = '60s'")
            cur.execute("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
        resp = await client.post("/api/system/run-migrations", headers=auth_header(admin_token))
        assert resp.status_code == 504, resp.text
        assert "1 database session(s) stopped" in resp.json()["detail"]
        with psycopg2.connect(_SYNC_DSN) as probe, probe.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'harbor-clerk-alembic-%' "
                "AND state = 'active'"
            )
            _left = cur.fetchone()[0]
        probe.close()
        assert _left == 0, "the migration's backend is still running after the cancel"
    finally:
        blocker.rollback()
        blocker.close()
