"""Add summarize pipeline stage: version_status and job_stage enum values.

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
    op.execute(sa.text("COMMIT"))
    op.execute(
        sa.text(
            "ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'summarizing' BEFORE 'finalizing'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'summarized' BEFORE 'finalizing'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TYPE job_stage ADD VALUE IF NOT EXISTS 'summarize' BEFORE 'finalize'"
        )
    )
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; they remain harmless.
    pass
