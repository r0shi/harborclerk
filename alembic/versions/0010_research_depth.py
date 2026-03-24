"""Add depth to research_state."""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_name='research_state' AND column_name='depth'")
    )
    if not result.fetchone():
        op.add_column("research_state", sa.Column("depth", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("research_state", "depth")
