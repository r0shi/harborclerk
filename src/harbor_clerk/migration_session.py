"""Name the database sessions an alembic run opens, so a caller that gives up on it can cancel them.

Killing an alembic client does not stop its backend: a statement already sent (a table rewrite
under ACCESS EXCLUSIVE, say) runs to its end and only then rolls back, holding its locks all the
while. ``alembic/env.py`` sets ``application_name`` from ``APPLICATION_NAME_ENV``; a caller that
runs alembic with a deadline sets a unique name and, past the deadline, cancels every backend
carrying it (``cancel_backends``). Imported by env.py, so it must not read settings at import. env.py also serialises runs with
``MIGRATION_LOCK_KEY``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

APPLICATION_NAME_ENV = "HC_MIGRATION_APPLICATION_NAME"
DEFAULT_APPLICATION_NAME = "harbor-clerk-alembic"

# Session-level advisory lock env.py holds for a whole run, so runs are serial: a second runner
# (the Mac app's MigrationRunner, the Compose entrypoint, `alembic current`/`stamp`, a test) waits
# for the one in progress and then finds the database at head; the maintenance route refuses
# instead (409). Two concurrent runs of 0009 otherwise rewrote chunks twice and the second failed
# to stamp. Waiters poll pg_try_advisory_lock outside a transaction, never pg_advisory_lock: a
# backend blocked in that holds a snapshot, which 0008's CREATE INDEX CONCURRENTLY waits for while
# the waiter waits for the build's lock (a deadlock). The bytes of "HC_MIGRT" as a bigint; fixed,
# never reuse it for anything else.
MIGRATION_LOCK_KEY = int.from_bytes(b"HC_MIGRT", "big")
MIGRATION_LOCK_POLL_SECONDS = 1.0


# The backend holding the migration lock in this database: (pid, application_name), no row if free.
# A bigint advisory key shows as its high half in classid and its low half in objid.
LOCK_HOLDER_SQL = (
    "SELECT l.pid, coalesce(a.application_name, '') FROM pg_locks l "
    "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND l.classid::bigint = CAST(:high AS bigint) AND l.objid::bigint = CAST(:low AS bigint) "
    "LIMIT 1"
)
LOCK_HOLDER_PARAMS = {"high": MIGRATION_LOCK_KEY >> 32, "low": MIGRATION_LOCK_KEY & 0xFFFFFFFF}


async def migration_lock_holder(session: AsyncSession) -> str | None:
    """The application_name of the backend holding the migration lock in this database ("" if it
    has none), or None if nobody holds it. Reads pg_locks; takes nothing."""
    row = (await session.execute(text(LOCK_HOLDER_SQL), LOCK_HOLDER_PARAMS)).first()
    return None if row is None else row[1]


def application_name() -> str:
    """The name env.py gives its connection: the caller's, or the default."""
    return os.environ.get(APPLICATION_NAME_ENV) or DEFAULT_APPLICATION_NAME


def unique_application_name() -> str:
    """A name no other session carries (application_name holds at most 63 bytes)."""
    return f"{DEFAULT_APPLICATION_NAME}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Cancelled:
    """What ``cancel_backends`` signalled: the client sessions (one per alembic run), and every
    backend including the parallel workers a build runs under its leader's name (an HNSW build
    showed three)."""

    sessions: int
    backends: int


async def cancel_backends(session: AsyncSession, name: str) -> Cancelled:
    """``pg_cancel_backend`` every other backend in this database whose application_name is ``name``."""
    row = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE cancelled AND backend_type = 'client backend'), "
                "count(*) FILTER (WHERE cancelled) "
                "FROM (SELECT backend_type, pg_cancel_backend(pid) AS cancelled FROM pg_stat_activity "
                "WHERE application_name = :name AND datname = current_database() "
                "AND pid <> pg_backend_pid()) AS signalled"
            ),
            {"name": name},
        )
    ).one()
    return Cancelled(sessions=int(row[0]), backends=int(row[1]))


async def wait_for_exit(session: AsyncSession, name: str, *, seconds: float = 10.0) -> bool:
    """Wait, at most ``seconds``, until no backend in this database carries ``name``; whether none does.

    A cancelled backend rolls back and exits on its own: until it has, what it wrote is not yet
    undone, and the locks it held are still held.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name "
                    "AND datname = current_database() AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
        ).scalar()
        await session.rollback()  # a fresh snapshot of pg_stat_activity on the next read
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)
