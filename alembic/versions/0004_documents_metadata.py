"""documents.metadata JSONB column

Adds a JSONB metadata column populated at ingest by the new extractor
framework (Tika headers, YAML frontmatter, JSON sidecars). GIN index
supports the @> containment operator used by kb_search's metadata_filter
parameter.

The SQLAlchemy attribute on Document is `doc_metadata` to avoid shadowing
Base.metadata; the PostgreSQL column is `metadata`.

Revision ID: 0004_documents_metadata
Revises: 0003_wf_skip_tracking
Create Date: 2026-05-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_documents_metadata"
down_revision: str | None = "0003_wf_skip_tracking"
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
        "documents",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_documents_metadata_gin",
        "documents",
        ["metadata"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_documents_metadata_gin", table_name="documents")
    op.drop_column("documents", "metadata")
