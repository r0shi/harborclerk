"""Name the database sessions an alembic run opens, so a caller that gives up on it can cancel them.

Killing an alembic client does not stop its backend: a statement already sent (a table rewrite
under ACCESS EXCLUSIVE, say) runs to its end and only then rolls back, holding its locks all the
while. ``alembic/env.py`` sets ``application_name`` from ``APPLICATION_NAME_ENV``; a caller that
runs alembic with a deadline sets a unique name and, past the deadline, cancels every backend
carrying it (``cancel_backends``). Imported by env.py, so it must not read settings at import.
"""

from __future__ import annotations

import os
import uuid

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


async def cancel_backends(session: AsyncSession, name: str) -> int:
    """``pg_cancel_backend`` every other backend whose application_name is ``name``; how many."""
    result = await session.execute(
        text(
            "SELECT count(*) FILTER (WHERE pg_cancel_backend(pid)) FROM pg_stat_activity "
            "WHERE application_name = :name AND pid <> pg_backend_pid()"
        ),
        {"name": name},
    )
    return int(result.scalar() or 0)
