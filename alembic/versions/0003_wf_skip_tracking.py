"""watched_folder skip tracking

Adds skipped_count + skipped_extensions to watched_folders. Phase 5 of the
markdown-handling feature: the watcher counts files it rejected for an
unsupported extension during the initial scan, so the UI can surface
"N files not ingested — unsupported types: .canvas, .excalidraw" per folder.

Revision ID: 0003_wf_skip_tracking
Revises: 0002_document_links
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_wf_skip_tracking"
down_revision: str | None = "0002_document_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# These four module-level names are read by alembic via attribute access
# (``module.revision`` etc.) rather than ordinary imports, so static
# analysis (CodeQL ``py/unused-global-variable``) doesn't see them as used.
# Declaring them in ``__all__`` marks them as the module's intentional
# public surface and silences the false positive.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    op.add_column(
        "watched_folders",
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "watched_folders",
        sa.Column(
            "skipped_extensions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("watched_folders", "skipped_extensions")
    op.drop_column("watched_folders", "skipped_count")
