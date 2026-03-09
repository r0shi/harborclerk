"""Add model_id column to chat_messages.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("model_id", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "model_id")
