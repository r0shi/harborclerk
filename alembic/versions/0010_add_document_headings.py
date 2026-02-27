"""Add document_headings table.

Stores heading hierarchy extracted from Tika XHTML during the extract stage.

Revision ID: 0010
Revises: 0009
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_headings",
        sa.Column(
            "heading_id",
            postgresql.UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "version_id",
            postgresql.UUID,
            sa.ForeignKey("document_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("page_num", sa.Integer, nullable=True),
        sa.Column("position", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_document_headings_version_id",
        "document_headings",
        ["version_id"],
    )


def downgrade() -> None:
    op.drop_table("document_headings")
