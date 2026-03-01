"""Switch embedding model to all-MiniLM-L6-v2 (384-dim).

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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

    # -- Re-queue embed+finalize so workers re-process with new model --
    op.execute(
        "UPDATE ingestion_jobs"
        " SET status = 'queued', started_at = NULL, finished_at = NULL,"
        "     progress_current = 0, progress_total = 0"
        " WHERE stage IN ('embed', 'finalize') AND status = 'done'"
    )
    op.execute("SELECT pg_notify('job_enqueued_cpu', '')")


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

    # -- Re-queue embed+finalize so workers re-process after dimension revert --
    op.execute(
        "UPDATE ingestion_jobs"
        " SET status = 'queued', started_at = NULL, finished_at = NULL,"
        "     progress_current = 0, progress_total = 0"
        " WHERE stage IN ('embed', 'finalize') AND status = 'done'"
    )
    op.execute("SELECT pg_notify('job_enqueued_cpu', '')")
