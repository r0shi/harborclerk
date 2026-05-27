"""chunks chunk_text trgm gin index

Adds a GIN trigram index on chunks.chunk_text to support fast
case-insensitive ILIKE filtering used by kb_find_all's text_contains
parameter (and kb_search's same parameter). Without this, text_contains
forces a seq scan on every search.

The pg_trgm extension is already enabled (see 0001_initial.py).

Revision ID: b56bde44ec8c
Revises: 0004_documents_metadata
Create Date: 2026-05-26 23:16:43.332803
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b56bde44ec8c"
down_revision = "0004_documents_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_chunk_text_trgm "
            "ON public.chunks USING gin (chunk_text public.gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_chunks_chunk_text_trgm")
