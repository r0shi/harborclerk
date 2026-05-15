"""Create imap_command_log table.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "imap_command_log",
        sa.Column(
            "log_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label_path", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("args_redacted", sa.Text(), nullable=True),
        sa.Column("response_status", sa.Text(), nullable=False),
        sa.Column("response_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_imap_command_log_account_time",
        "imap_command_log",
        ["account_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_imap_command_log_created",
        "imap_command_log",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_imap_command_log_created", table_name="imap_command_log")
    op.drop_index("ix_imap_command_log_account_time", table_name="imap_command_log")
    op.drop_table("imap_command_log")
