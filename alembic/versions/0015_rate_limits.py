"""Add rate_limit_rpm and rate_limit_rph to api_keys.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("rate_limit_rpm", sa.Integer(), nullable=True))
    op.add_column("api_keys", sa.Column("rate_limit_rph", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "rate_limit_rph")
    op.drop_column("api_keys", "rate_limit_rpm")
