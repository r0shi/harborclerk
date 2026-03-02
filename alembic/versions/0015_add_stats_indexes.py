"""Add indexes for stats dashboard queries.

- entities(entity_text, chunk_id): co-occurrence self-join (entity_text IN + chunk_id join)
- chunks(doc_id) WHERE embedding IS NOT NULL: cluster centroid aggregation

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Entity co-occurrence: filter by entity_text IN (...) then join on chunk_id
    op.create_index("ix_entities_text_chunk", "entities", ["entity_text", "chunk_id"])

    # Cluster centroid: GROUP BY doc_id over non-null embeddings only
    op.execute("CREATE INDEX chunks_doc_embedding_idx ON chunks (doc_id) WHERE embedding IS NOT NULL")


def downgrade() -> None:
    op.drop_index("chunks_doc_embedding_idx", table_name="chunks")
    op.drop_index("ix_entities_text_chunk", table_name="entities")
