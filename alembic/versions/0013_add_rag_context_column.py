"""Add rag_context JSONB column to chat_messages."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("rag_context", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "rag_context")
