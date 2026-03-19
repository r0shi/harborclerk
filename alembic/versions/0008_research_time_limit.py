"""Add time_limit_minutes to research_state."""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='research_state' AND column_name='time_limit_minutes'"
        )
    )
    if not result.fetchone():
        op.add_column("research_state", sa.Column("time_limit_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_state", "time_limit_minutes")
