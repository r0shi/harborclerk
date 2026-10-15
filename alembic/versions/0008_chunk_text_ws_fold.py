r"""whitespace-folded trigram index for text_contains

``text_contains`` matched ``chunk_text ILIKE '%phrase%'``, so a phrase matched only where the stored
text had the same whitespace. PDF extraction leaves non-breaking spaces and line breaks inside
phrases ("right\u00a0of\u00a0first\u00a0refusal", "first\nopportunity"), and those chunks never
matched the plain phrase (#819). The filter now compares whitespace-folded text on both sides:
``hc_fold_whitespace(chunk_text) ILIKE '%<folded phrase>%'``.

``hc_fold_whitespace`` turns every run of Unicode White_Space (tab, LF, VT, FF, CR, space, U+0085,
U+00A0, U+1680, U+2000..U+200A, U+2028, U+2029, U+202F, U+205F, U+3000) into one ASCII space. Its
pattern is the string ``harbor_clerk.text_contains.WHITESPACE_RUN_PATTERN`` produces, frozen here
and held to it by a test. It is IMMUTABLE (regexp_replace with explicit code points and no flags
does not depend on locale), so it can carry an expression index; it is a plain SQL function with
no SET clause so the planner inlines it on both the index and the query side.

Never redefine the function in place: an expression index computed with an old body silently
returns wrong answers under a new one. A change is a new function name (or a REINDEX in the same
migration).

The expression index ``ix_chunks_chunk_text_ws_trgm`` replaces ``ix_chunks_chunk_text_trgm``
(b56bde44ec8c), whose only reader was the unfolded predicate: keeping both would double the
trigram storage for an index nothing uses. Downgrade restores the plain index.

A stored generated column holding the folded text was the alternative; on a 40,000-chunk synthetic
corpus it took the table from 105 MB to 211 MB for an index of the same size (81 MB) as the
expression index, which adds no table storage.

Revision ID: 0008_chunk_text_ws_fold
Revises: 0007_ingestion_job_pipeline_seq
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_chunk_text_ws_fold"
down_revision: str | None = "0007_ingestion_job_pipeline_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

# Frozen copy of harbor_clerk.text_contains.WHITESPACE_RUN_PATTERN (Postgres ARE \uXXXX escapes).
WHITESPACE_RUN_PATTERN = r"[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028-\u2029\u202f\u205f\u3000]+"


def upgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION public.hc_fold_whitespace(text) RETURNS text "
        "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE "
        f"RETURN pg_catalog.regexp_replace($1, '{WHITESPACE_RUN_PATTERN}', ' ', 'g')"
    )
    # CONCURRENTLY cannot run inside a transaction; the function above commits first.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_chunk_text_ws_trgm "
            "ON public.chunks USING gin (public.hc_fold_whitespace(chunk_text) public.gin_trgm_ops)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_chunks_chunk_text_trgm")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_chunk_text_trgm "
            "ON public.chunks USING gin (chunk_text public.gin_trgm_ops)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_chunks_chunk_text_ws_trgm")
    op.execute("DROP FUNCTION IF EXISTS public.hc_fold_whitespace(text)")
