"""Schema sentinel verification — refuse to run when DB and binary disagree."""

from __future__ import annotations

import logging
import sys

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

import harbor_clerk.config as _config

logger = logging.getLogger(__name__)


class SchemaSentinelMismatch(RuntimeError):
    """Raised when schema_metadata sentinel rows don't match the binary's settings."""


async def verify_schema_sentinel(session: AsyncSession) -> None:
    """Compare schema_metadata sentinel rows to current settings.

    Raises ``SchemaSentinelMismatch`` if any of:
    - the ``schema_metadata`` table doesn't exist
    - the ``embed_model`` row is missing or differs from ``settings.embed_model``
    - the ``embed_dim`` row is missing or differs from ``settings.embed_dim``

    Caller decides whether to ``sys.exit(2)``; see ``panic_on_sentinel_mismatch``.
    """
    settings = _config.get_settings()
    try:
        result = await session.execute(
            text("SELECT key, value FROM schema_metadata WHERE key IN ('embed_model', 'embed_dim')")
        )
    except ProgrammingError as exc:
        raise SchemaSentinelMismatch(
            "schema_metadata table is missing. "
            "This DB has not been migrated to embedding-v2. "
            "Either drop the database and re-launch (Path A) "
            "or run scripts/migrate_to_embedding_v2.py (Path B). "
            "See docs/upgrade-runbook.md#embedding-v2."
        ) from exc

    rows = {key: value for key, value in result.all()}
    mismatches: list[str] = []
    found_model = rows.get("embed_model", "<missing>")
    if found_model != settings.embed_model:
        mismatches.append(f"  embed_model: expected={settings.embed_model!r}, found={found_model!r}")
    found_dim = rows.get("embed_dim", "<missing>")
    if found_dim != str(settings.embed_dim):
        mismatches.append(f"  embed_dim:   expected={settings.embed_dim!r}, found={found_dim!r}")

    if mismatches:
        raise SchemaSentinelMismatch(
            "Schema sentinel mismatch — refusing to start.\n"
            + "\n".join(mismatches)
            + "\nThis binary requires the embedding-v2 schema. Either:\n"
            "  1. Drop and recreate the database (fresh schema, then re-ingest via watched folders), OR\n"
            "  2. Run scripts/migrate_to_embedding_v2.py against this DB.\n"
            "See docs/upgrade-runbook.md#embedding-v2 for details."
        )


async def panic_on_sentinel_mismatch(session: AsyncSession) -> None:
    """Call verify_schema_sentinel; on failure log CRITICAL and sys.exit(2)."""
    try:
        await verify_schema_sentinel(session)
    except SchemaSentinelMismatch as exc:
        logger.critical("%s", exc)
        sys.exit(2)
