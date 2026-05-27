"""user scope columns on conversations and research_state

Adds a `scope` JSONB NOT NULL DEFAULT '{}' column to both tables, holding
the user's folder-filter selection per conversation/run. Forward-compatible
wrapper object — new scope axes (collection_ids, doc_ids, topic_ids) are
additive JSON keys with no further DDL.

Revision ID: 0005_user_scope_columns
Revises: d65a570f594c
Create Date: 2026-05-27 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0005_user_scope_columns"
down_revision = "d65a570f594c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "research_state",
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_state", "scope")
    op.drop_column("conversations", "scope")
