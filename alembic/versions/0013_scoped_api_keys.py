"""Add scoping fields to api_keys.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("permission_tier", sa.Text(), nullable=False, server_default="full"),
    )
    op.add_column(
        "api_keys",
        sa.Column("tool_overrides", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column("api_keys", sa.Column("scope_topic_ids", postgresql.JSONB(), nullable=True))
    op.add_column("api_keys", sa.Column("scope_folder_ids", postgresql.JSONB(), nullable=True))
    op.add_column("api_keys", sa.Column("max_snippet_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "max_snippet_chars")
    op.drop_column("api_keys", "scope_folder_ids")
    op.drop_column("api_keys", "scope_topic_ids")
    op.drop_column("api_keys", "tool_overrides")
    op.drop_column("api_keys", "permission_tier")
    op.drop_column("api_keys", "expires_at")
