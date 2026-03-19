"""Add time_limit_minutes to research_state."""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_state", sa.Column("time_limit_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_state", "time_limit_minutes")
