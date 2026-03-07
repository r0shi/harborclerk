"""Add doc_type column to document_versions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("doc_type", sa.Text(), nullable=True))
    op.create_index("ix_versions_doc_type", "document_versions", ["doc_type"])


def downgrade() -> None:
    op.drop_index("ix_versions_doc_type", table_name="document_versions")
    op.drop_column("document_versions", "doc_type")
