"""Add source_path to document_versions, add 'finalizing' version status.

Revision ID: 0006
Revises: 0005
Create Date: 2026-02-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("source_path", sa.Text, nullable=True))
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
    # Commit the current transaction, add the enum value, then resume.
    op.execute(sa.text("COMMIT"))
    op.execute(
        sa.text(
            "ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'finalizing' BEFORE 'ready'"
        )
    )
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    op.drop_column("document_versions", "source_path")
    # PostgreSQL does not support removing enum values; 'finalizing' remains harmless.
