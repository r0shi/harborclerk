"""whitespace-folded trigram index for text_contains

``text_contains`` matched ``chunk_text ILIKE '%phrase%'``, so a phrase matched only where the stored
text had the same whitespace. PDF extraction leaves non-breaking spaces (U+00A0) and line breaks
inside phrases ("right of first refusal" joined by NBSPs, "first" LF "opportunity"), and those chunks
never matched the plain phrase (#819). The filter now compares whitespace-folded text on both sides:
``hc_fold_whitespace(chunk_text) ILIKE '%<folded phrase>%'``.

``hc_fold_whitespace`` turns every run of Unicode White_Space (tab, LF, VT, FF, CR, space, U+0085,
U+00A0, U+1680, U+2000..U+200A, U+2028, U+2029, U+202F, U+205F, U+3000) into one ASCII space. Its
pattern is frozen below. It is IMMUTABLE (regexp_replace with explicit code points and no flags
does not depend on locale), so it can carry an expression index; it is a plain SQL function with
no SET clause so the planner inlines it on both the index and the query side. It is created only
if absent; an existing function with a different definition (body, volatility, strictness,
parallel safety, SECURITY DEFINER, SET options) is an error, never replaced (see
alembic/AGENTS.md: an expression index computed with an old body goes stale).

The expression index ``ix_chunks_chunk_text_ws_trgm`` replaces ``ix_chunks_chunk_text_trgm``
(b56bde44ec8c), whose only reader was the unfolded predicate. Each concurrent build first drops an
INVALID leftover of the same name (an interrupted CREATE INDEX CONCURRENTLY leaves one, and
``IF NOT EXISTS`` would skip it), and the index it replaces is dropped only once the new one is
valid. A leftover whose build is still running (its client was killed, its backend was not) is
not dropped: the migration raises and names the pid, to be retried when that build finishes.
Downgrade restores the plain index the same way. An interrupted downgrade followed by
``upgrade head``, rather than by another downgrade, leaves the fold index INVALID at 0008 (an
interrupted DROP INDEX CONCURRENTLY marks it INVALID, and the version is still 0008, so the
upgrade does nothing): re-run the downgrade, then upgrade.

A stored generated column holding the folded text was the alternative; on a 40,000-chunk synthetic
corpus it took the table from 105 MB to 211 MB for an index of the same size (81 MB) as the
expression index, which adds no table storage.

Revision ID: 0008_chunk_text_ws_fold
Revises: 0007_ingestion_job_pipeline_seq
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_chunk_text_ws_fold"
down_revision: str | None = "0007_ingestion_job_pipeline_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

# One run of Unicode White_Space, as a Postgres ARE with backslash-u escapes. Frozen: never edit.
WHITESPACE_RUN_PATTERN = r"[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028-\u2029\u202f\u205f\u3000]+"

FOLD_FUNCTION = "public.hc_fold_whitespace"
FOLD_INDEX = "ix_chunks_chunk_text_ws_trgm"
PLAIN_INDEX = "ix_chunks_chunk_text_trgm"

_FUNCTION_DDL = (
    "CREATE FUNCTION {name}(text) RETURNS text "
    "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE "
    f"RETURN pg_catalog.regexp_replace($1, '{WHITESPACE_RUN_PATTERN}', ' ', 'g')"
)
_EXPECTED_FUNCTION = "pg_temp.hc_fold_whitespace_expected"

_FOLD_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {FOLD_INDEX} "
    f"ON public.chunks USING gin ({FOLD_FUNCTION}(chunk_text) public.gin_trgm_ops)"
)
_PLAIN_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {PLAIN_INDEX} ON public.chunks USING gin (chunk_text public.gin_trgm_ops)"
)


def _create_fold_function(bind) -> None:
    """Create the function if absent; if present, require the same definition (a re-run after an
    interrupted index build finds it), and refuse to touch one that differs."""
    if bind.execute(sa.text(f"SELECT to_regprocedure('{FOLD_FUNCTION}(text)')")).scalar() is None:
        bind.execute(sa.text(_FUNCTION_DDL.format(name=FOLD_FUNCTION)))
        return
    bind.execute(sa.text(_FUNCTION_DDL.format(name=_EXPECTED_FUNCTION)))
    same = bind.execute(
        sa.text(
            # A same-body function with SET options or SECURITY DEFINER no longer inlines (the index
            # stops matching); PARALLEL UNSAFE forces serial plans. The twin's body is never NULL, so
            # an old-style (AS $$...$$) function, whose sqlbody is NULL, compares unequal.
            "SELECT pg_get_function_sqlbody(a.oid) IS NOT DISTINCT FROM pg_get_function_sqlbody(b.oid) "
            "AND a.provolatile = b.provolatile AND a.proisstrict = b.proisstrict "
            "AND a.proparallel = b.proparallel AND a.prosecdef = b.prosecdef "
            "AND a.proconfig IS NOT DISTINCT FROM b.proconfig "
            "AND a.prorettype = b.prorettype "
            f"FROM pg_proc a, pg_proc b WHERE a.oid = '{FOLD_FUNCTION}(text)'::regprocedure "
            f"AND b.oid = '{_EXPECTED_FUNCTION}(text)'::regprocedure"
        )
    ).scalar()
    bind.execute(sa.text(f"DROP FUNCTION {_EXPECTED_FUNCTION}(text)"))
    if not same:
        raise RuntimeError(
            f"{FOLD_FUNCTION}(text) exists with a different definition; an expression index built on it "
            "would be stale. A changed fold is a new function name in a new migration (alembic/AGENTS.md)."
        )


def _index_valid(bind, name: str) -> bool | None:
    """True/False for a valid/INVALID index named ``name`` in public; None if there is none."""
    return bind.execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = :name"
        ),
        {"name": name},
    ).scalar()


def _require_valid(bind, name: str) -> None:
    if _index_valid(bind, name) is not True:
        raise RuntimeError(
            f"index public.{name} is missing or INVALID after CREATE INDEX CONCURRENTLY; "
            "refusing to drop the index it replaces"
        )


def _build_in_progress(bind, name: str) -> int | None:
    """The pid of a backend still running CREATE INDEX (CONCURRENTLY) on public.<name>, if any."""
    return bind.execute(
        sa.text(
            "SELECT p.pid FROM pg_stat_progress_create_index p JOIN pg_class c ON c.oid = p.index_relid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = :name "
            "LIMIT 1"
        ),
        {"name": name},
    ).scalar()


def _build_index_concurrently(bind, name: str, ddl: str) -> None:
    """CREATE INDEX CONCURRENTLY that survives an interrupted earlier attempt: an INVALID leftover
    of the same name is dropped first (IF NOT EXISTS would otherwise skip it), and the result must
    be valid before the caller drops anything. ``bind`` must be in autocommit (autocommit_block)."""
    if _index_valid(bind, name) is False:
        # An orphaned build (client killed, backend still running) also shows as INVALID until it
        # finishes; dropping it would throw that work away and start again.
        pid = _build_in_progress(bind, name)
        if pid is not None:
            raise RuntimeError(f"index build in progress (pid {pid}) on public.{name}; retry when it finishes")
        bind.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}"))
    bind.execute(sa.text(ddl))
    _require_valid(bind, name)


def upgrade() -> None:
    _create_fold_function(op.get_bind())
    # CONCURRENTLY cannot run inside a transaction; the function above commits first.
    with op.get_context().autocommit_block():
        _build_index_concurrently(op.get_bind(), FOLD_INDEX, _FOLD_INDEX_DDL)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{PLAIN_INDEX}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _build_index_concurrently(op.get_bind(), PLAIN_INDEX, _PLAIN_INDEX_DDL)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{FOLD_INDEX}")
    op.execute(f"DROP FUNCTION IF EXISTS {FOLD_FUNCTION}(text)")
