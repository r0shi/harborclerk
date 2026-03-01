"""Add indexes for corpus overview and cross-document similarity queries.

- documents(status, updated_at DESC): corpus overview filtering + sorting
- chunks(language): corpus overview language distribution GROUP BY

Revision ID: 0011
Revises: 0010
Create Date: 2026-02-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE INDEX documents_status_updated_idx ON documents (status, updated_at DESC)")
    op.create_index("chunks_language_idx", "chunks", ["language"])


def downgrade() -> None:
    op.drop_index("chunks_language_idx", table_name="chunks")
    op.drop_index("documents_status_updated_idx", table_name="documents")
