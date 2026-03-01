"""Add rag_context JSONB column to chat_messages."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("rag_context", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "rag_context")
