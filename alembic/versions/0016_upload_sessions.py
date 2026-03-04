"""Add upload_sessions table and session_id/source_path to uploads.

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_sessions",
        sa.Column(
            "session_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("auto_confirm", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("total_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column("uploads", sa.Column("session_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_uploads_session_id", "uploads", "upload_sessions", ["session_id"], ["session_id"], ondelete="SET NULL"
    )
    op.create_index("ix_uploads_session_id", "uploads", ["session_id"])
    op.add_column("uploads", sa.Column("source_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_uploads_session_id", "uploads", if_exists=True)
    op.drop_constraint("fk_uploads_session_id", "uploads", type_="foreignkey")
    op.drop_column("uploads", "source_path")
    op.drop_column("uploads", "session_id")
    op.drop_table("upload_sessions")
