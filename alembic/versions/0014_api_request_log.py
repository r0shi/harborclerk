"""Create api_request_log table.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_request_log",
        sa.Column(
            "request_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.key_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("request_type", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_api_request_log_key_time_ep",
        "api_request_log",
        ["api_key_id", sa.text("created_at DESC"), "endpoint"],
    )
    op.create_index(
        "ix_api_request_log_created",
        "api_request_log",
        ["created_at"],
    )
    op.create_index(
        "ix_api_request_log_endpoint",
        "api_request_log",
        ["endpoint"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_request_log_endpoint", table_name="api_request_log")
    op.drop_index("ix_api_request_log_created", table_name="api_request_log")
    op.drop_index("ix_api_request_log_key_time_ep", table_name="api_request_log")
    op.drop_table("api_request_log")
