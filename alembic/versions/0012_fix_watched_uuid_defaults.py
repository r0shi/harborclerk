"""Fix missing server defaults on watched_folders.folder_id and watched_files.file_id.

Migration 0011 omitted gen_random_uuid() defaults on the UUID PKs.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "watched_folders",
        "folder_id",
        server_default=sa.text("gen_random_uuid()"),
    )
    op.alter_column(
        "watched_files",
        "file_id",
        server_default=sa.text("gen_random_uuid()"),
    )


def downgrade() -> None:
    op.alter_column("watched_files", "file_id", server_default=None)
    op.alter_column("watched_folders", "folder_id", server_default=None)
