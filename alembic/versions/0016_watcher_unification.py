"""Watcher unification: nullable bookmark_data + display_name + unavailable_reason + auto_discovered.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("watched_folders", "bookmark_data", nullable=True)
    op.add_column("watched_folders", sa.Column("unavailable_reason", sa.Text(), nullable=True))
    op.add_column("watched_folders", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column(
        "watched_folders",
        sa.Column("auto_discovered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("watched_folders", "auto_discovered")
    op.drop_column("watched_folders", "display_name")
    op.drop_column("watched_folders", "unavailable_reason")
    op.alter_column("watched_folders", "bookmark_data", nullable=False)
