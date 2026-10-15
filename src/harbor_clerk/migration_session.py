"""Name the database sessions an alembic run opens, so a caller that gives up on it can cancel them.

Killing an alembic client does not stop its backend: a statement already sent (a table rewrite
under ACCESS EXCLUSIVE, say) runs to its end and only then rolls back, holding its locks all the
while. ``alembic/env.py`` sets ``application_name`` from ``APPLICATION_NAME_ENV``; a caller that
runs alembic with a deadline sets a unique name and, past the deadline, cancels every backend
carrying it (``cancel_backends``). Imported by env.py, so it must not read settings at import.
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
