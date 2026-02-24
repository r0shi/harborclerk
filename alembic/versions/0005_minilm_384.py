"""Switch embedding model to all-MiniLM-L6-v2 (384-dim).

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-24
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Embedding dimension 768 → 384 --
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


def downgrade() -> None:
    op.drop_index("chunks_embedding_hnsw_idx", table_name="chunks")
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
