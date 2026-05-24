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

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_documents_metadata"
down_revision = "0003_wf_skip_tracking"
branch_labels = None
depends_on = None


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
