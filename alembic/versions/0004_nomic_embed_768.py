"""Switch embedding model to nomic-embed-text-v1.5 (768-dim), add heartbeat_at.

Revision ID: 0004
Revises: 0003
Create Date: 2026-02-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Embedding dimension 384 → 768 --
    op.drop_index("chunks_embedding_hnsw_idx", table_name="chunks")
    # NULL out existing 384-dim vectors (incompatible with new dimension)
    op.execute("UPDATE chunks SET embedding = NULL")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768)")
    op.create_index(
        "chunks_embedding_hnsw_idx",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # -- Worker heartbeat --
    op.add_column(
        "ingestion_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "heartbeat_at")

    op.drop_index("chunks_embedding_hnsw_idx", table_name="chunks")
    op.execute("UPDATE chunks SET embedding = NULL")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(384)")
    op.create_index(
        "chunks_embedding_hnsw_idx",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
