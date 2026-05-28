"""Backfill documents.mime_type from canonical_filename for legacy rows.

Pre this migration, the watcher (the only ingest path post Stage-2
watched-folder-first refactor) never set ``mime_type`` on the Document
rows it created. As a result every document on disk had NULL mime_type
and the Observatory's Document Clusters file-type breakdown degraded to
"Unknown" across the board.

This migration backfills mime_type for any NULL row using the same
extension guesser the watcher now uses for new ingests
(``harbor_clerk.file_types.guess_mime_type``). The watcher fix sits in
its own commit; this migration handles the existing corpus.

Idempotent: only touches rows where ``mime_type IS NULL``. Safe to re-run.

Revision ID: 0006_backfill_mime_type
Revises: 0005_user_scope_columns
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from harbor_clerk.file_types import guess_mime_type

revision: str = "0006_backfill_mime_type"
down_revision: str | None = "0005_user_scope_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# These four module-level names are read by alembic via attribute access
# rather than ordinary imports, so static analysis doesn't see them as used.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    # Offline (--sql) mode can't enumerate rows to compute per-doc mime
    # values, so emit a comment and bail. The real upgrade path runs
    # online from the menubar app at startup.
    if op.get_context().as_sql:
        op.execute("-- 0006_backfill_mime_type: skipped in offline mode (requires live SELECT)")
        return

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT doc_id, canonical_filename FROM documents "
            "WHERE mime_type IS NULL AND canonical_filename IS NOT NULL"
        )
    ).fetchall()
    if not rows:
        return

    # Group by guessed mime so we issue one UPDATE per distinct type
    # (typically 20-30) rather than one per row (potentially 10k+).
    by_mime: dict[str, list[str]] = {}
    for doc_id, fname in rows:
        mt = guess_mime_type(fname)
        by_mime.setdefault(mt, []).append(doc_id)

    for mt, ids in by_mime.items():
        bind.execute(
            sa.text("UPDATE documents SET mime_type = :mt WHERE doc_id = ANY(:ids) AND mime_type IS NULL"),
            {"mt": mt, "ids": ids},
        )


def downgrade() -> None:
    # No-op: backfilled values are indistinguishable from naturally
    # populated ones, and the column itself was added in a much earlier
    # migration. Reverting would have to discriminate the two sets, which
    # we cannot do.
    pass
