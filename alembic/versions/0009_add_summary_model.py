"""Add summary_model column to document_versions.

Tracks which model (or 'extractive' fallback) produced the summary.

Revision ID: 0009
Revises: 0008
Create Date: 2026-02-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("summary_model", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_versions", "summary_model")
