"""Audit logging for IMAP commands.

Every IMAP command issued by the sync engine is recorded in the
imap_command_log table — verb, redacted args, response status,
response size, duration. LOGIN and XOAUTH2 credentials are masked
before storage; response bodies are never persisted (only the byte
count).

A periodic reaper drops rows older than 30 days.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import ImapCommandLog

# Commands whose arguments contain credentials.
_CREDENTIAL_COMMANDS = frozenset({"LOGIN", "XOAUTH2"})

# How long to retain audit rows. Incident-driven access pattern — 30
# days is enough for most postmortems; users worried about a longer
# window can raise this.
RETENTION_DAYS = 30


def redact_imap_args(command: str, args: tuple) -> str:
    """Format command arguments for storage, masking credentials.

    For LOGIN and XOAUTH2 the password / token (last positional arg)
    is replaced with the literal '[redacted]'. All other commands
    are passed through with simple space-joined string formatting.
    """
    cmd = command.upper()
    if cmd in _CREDENTIAL_COMMANDS and args:
        safe = list(args[:-1]) + ["[redacted]"]
        return " ".join(str(a) for a in safe)
    return " ".join(str(a) for a in args)


async def log_imap_command(
    session: AsyncSession,
    *,
    account_id: UUID,
    label_path: str | None,
    command: str,
    args: tuple,
    response_status: str,
    response_bytes: int,
    duration_ms: int,
    error: str | None,
) -> None:
    """Insert one audit row. Caller is responsible for the surrounding
    transaction (flush/commit). Errors here must not break the IMAP
    operation that's being audited — callers should wrap and log."""
    session.add(
        ImapCommandLog(
            account_id=account_id,
            label_path=label_path,
            command=command.upper(),
            args_redacted=redact_imap_args(command, args),
            response_status=response_status,
            response_bytes=response_bytes,
            duration_ms=duration_ms,
            error=error,
        )
    )


@asynccontextmanager
async def audit_session_scope() -> AsyncIterator[AsyncSession]:
    """Short-lived session for IMAP audit writes. Commits on exit.

    Use one of these per per-label sync task so audit writes commit
    independently of the sync engine's transactional session. If the
    sync rolls back, the log of what was attempted is preserved.
    """
    from harbor_clerk.db import async_session_factory  # lazy import avoids circular deps

    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def reap_old_imap_command_logs(session: AsyncSession, *, retention_days: int = RETENTION_DAYS) -> int:
    """Delete rows older than retention_days. Returns the number of
    rows deleted (for logging / metrics)."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await session.execute(delete(ImapCommandLog).where(ImapCommandLog.created_at < cutoff))
    return result.rowcount or 0
