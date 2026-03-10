"""Add research mode tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("mode", sa.String(10), nullable=False, server_default="chat"),
    )

    op.create_table(
        "research_state",
        sa.Column(
            "conversation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("strategy", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("current_round", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_rounds", sa.Integer, nullable=False),
        sa.Column("progress", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("error", sa.Text, nullable=True),
    )

    # Enforce at most one running research task at a time
    op.execute("CREATE UNIQUE INDEX ix_research_state_one_running ON research_state (status) WHERE status = 'running'")

    op.create_table(
        "model_settings",
        sa.Column("model_id", sa.String(50), primary_key=True),
        sa.Column(
            "settings",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_settings")
    op.execute("DROP INDEX IF EXISTS ix_research_state_one_running")
    op.drop_table("research_state")
    op.drop_column("conversations", "mode")
