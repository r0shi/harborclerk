"""Add research_state.citations — docs that informed the research answer.

The research engine does retrieval internally and never writes tool-result
messages, so the read-time citation extraction in routes/research.py always
produced an empty list. The engine now persists the "docs that informed the
answer" set here instead.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-22

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_state",
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_state", "citations")
