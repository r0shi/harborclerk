"""Add summary column to document_versions.

Revision ID: 0007
Revises: 0006
Create Date: 2026-02-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("summary", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("document_versions", "summary")
