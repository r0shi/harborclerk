"""full-text columns over whitespace-folded chunk text

``chunks.fts_en`` and ``chunks.fts_fr`` were ``to_tsvector(<config>, coalesce(chunk_text, ''))``.
Under LC_CTYPE C (the Mac app's cluster, ``initdb --locale=C``) the text search parser reads a
non-breaking space (U+00A0), a narrow no-break space (U+202F) and the other non-ASCII spaces as
word characters, so "right of first refusal" joined by NBSPs was one lexeme and
``websearch_to_tsquery('first refusal')`` never matched it: those chunks reached hybrid search
only through the vector leg (#825). The columns are now computed over
``public.hc_fold_whitespace(...)`` (0008), which turns every run of Unicode White_Space into one
ASCII space, so the lexemes are the same in every database locale.

``ALTER TABLE ... ALTER COLUMN ... SET EXPRESSION`` (PostgreSQL 17+; every deployment runs 18)
recomputes a stored generated column by rewriting the table under an ACCESS EXCLUSIVE lock, and a
rewrite rebuilds every index on ``chunks`` (the HNSW embedding index, the trigram index and both
FTS GIN indexes included). Both columns change in one ALTER TABLE, so it is one rewrite, followed
by ANALYZE of those two columns: SET EXPRESSION drops their statistics (only theirs), and
autovacuum would re-analyze only after about 10% churn. The rewrite needs disk headroom for a second copy of ``chunks`` and its indexes
(416 MB at 40,000 chunks); on a full disk the upgrade fails with ENOSPC, rolls back, and the
runner retries.

env.py holds a session-level advisory lock for the whole run, so runs are serial: a second runner
(a restart during a run, ``alembic current``) waits for the first and then finds the database at
head, rather than rewriting the table a second time; the Run Migrations route refuses with 409
instead of starting one. A waiter polls ``pg_try_advisory_lock`` once a second outside any
transaction: blocked in ``pg_advisory_lock`` it would hold a snapshot, which 0008's CREATE INDEX
CONCURRENTLY waits for while the waiter waits for the build's lock, a deadlock in which the
detector killed one of the two runs.

It runs in the migration's transaction, so an interrupted run rolls back whole, with nothing
half-built for the retry to trip over. env.py runs every pending revision in one transaction, and
0008's autocommit block commits only its own work: a run interrupted in 0009 that started at 0008
stays at 0008, and one that started below 0008 stays at 0007 (with 0008's function and index
committed, which 0008 accepts on the retry). Either way the retry is harmless.

Measured on a private PostgreSQL 18.3 database (LC_CTYPE C, M-series Mac) with 40,000 synthetic
chunks (840 characters on average, mixed ASCII spaces, NBSP, U+202F and line breaks, 70% English
and 30% French, every chunk embedded): the upgrade took 26-28 s and held ACCESS EXCLUSIVE on
``chunks`` for 25-27 s of it; the downgrade 25 s, lock 24 s. About 16 s of that is rebuilding the
HNSW index (REINDEX of it alone: 16 s; without it the upgrade took 11-12 s). The time grows with
the chunk count: the CUAD sample's 6,408 chunks are a few seconds. The rewrite also rebuilds an
INVALID index left by an interrupted concurrent build (making it valid); 0008's leftovers are built
from their real definitions, so that is harmless.

While it runs, nothing else can read or write ``chunks``, and the lock lasts for the whole rewrite
unless the backend running it is cancelled: killing the alembic client does not stop its backend,
which finishes the rewrite and only then rolls back. The Mac app runs migrations after PostgreSQL
starts and before the API and workers (``ServiceManager.startAll`` -> ``MigrationRunner``), so
nothing is waiting: the app is simply slower to come up once. In Compose the API container
migrates before it serves, and the workers wait on its health check (``start_period`` 600 s, so
a long migration does not mark it unhealthy; one longer than about 650 s still leaves the
dependents failed until ``docker compose up`` is run again, and a broken API now takes up to 10
minutes to be reported unhealthy). The "Run Migrations" maintenance action runs alembic
beside a live API with a 120 s limit. The ALTER waits at most 10 s for its lock
(``lock_timeout``) and otherwise fails, saying another transaction holds ``chunks``, rather than
queueing every search behind it; a search during the rewrite waits for it, and at the limit
the route kills alembic and cancels its backends by their ``application_name``
(``harbor_clerk.migration_session``), as it does when the request task is cancelled in-process,
so the transaction rolls back at once instead of holding the lock to the end. (A client disconnect
does not cancel the request; after a SIGKILL of the API the orphaned alembic finishes on its own,
and the next runner waits for its lock.) It reports the revision
the database is at (a run that started below 0008 keeps 0008's committed function and index but
stays at 0007), or that the migration completed if the deadline fell after its commit. Above
roughly 180,000 chunks this migration does not fit in that limit; restart the app (or the API
container) to run it.

The columns now depend on ``hc_fold_whitespace``, exactly as ``ix_chunks_chunk_text_ws_trgm``
does: a stored value is computed with the body of the day it was written, so the function is
never redefined in place (alembic/AGENTS.md), and ``DROP FUNCTION ... CASCADE`` would drop both
columns. This migration reuses 0008's check: it refuses to build on a function whose definition
differs from 0008's (creating it if it is absent, as 0008 would).

Downgrade restores the unfolded expressions, with the same rewrite.

Revision ID: 0009_fts_over_folded_text
Revises: 0008_chunk_text_ws_fold
Create Date: 2026-10-15
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0009_fts_over_folded_text"
down_revision: str | None = "0008_chunk_text_ws_fold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

# The model's Computed(...) expressions (models/chunk.py) are these, verbatim;
# tests/test_fts_folded_text.py holds the live columns to the model.
FOLDED_EXPRESSIONS = {
    "fts_en": "to_tsvector('english', public.hc_fold_whitespace(coalesce(chunk_text, '')))",
    "fts_fr": "to_tsvector('french', public.hc_fold_whitespace(coalesce(chunk_text, '')))",
}
# 0001_initial's expressions.
PLAIN_EXPRESSIONS = {
    "fts_en": "to_tsvector('english', coalesce(chunk_text, ''))",
    "fts_fr": "to_tsvector('french', coalesce(chunk_text, ''))",
}


def _require_fold_function() -> None:
    """0008's guard, not a copy of it: the function exists with 0008's exact definition."""
    path = Path(__file__).with_name("0008_chunk_text_ws_fold.py")
    spec = importlib.util.spec_from_file_location("_hc_migration_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._create_fold_function(op.get_bind())


# How long the ALTER may wait for ACCESS EXCLUSIVE on chunks. At startup (the Mac app, the Compose
# entrypoint) nothing else holds the table; on a live API (the Run Migrations route) a long wait
# would queue every search behind the ALTER, so it fails fast instead.
ALTER_LOCK_TIMEOUT = "10s"
LOCK_NOT_AVAILABLE = "55P03"


def _set_expressions(expressions: dict[str, str]) -> None:
    clauses = ", ".join(f"ALTER COLUMN {column} SET EXPRESSION AS ({expr})" for column, expr in expressions.items())
    op.execute(f"SET LOCAL lock_timeout = '{ALTER_LOCK_TIMEOUT}'")
    try:
        op.execute(f"ALTER TABLE public.chunks {clauses}")
    except sa.exc.DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"0009 could not lock public.chunks within {ALTER_LOCK_TIMEOUT}: another transaction holds it "
            "(a search or a worker on a running API). Retry when it is idle, or restart the app (or the API "
            "container) so the migration runs at startup, before anything else opens the table."
        ) from exc
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def upgrade() -> None:
    _require_fold_function()
    _set_expressions(FOLDED_EXPRESSIONS)
    # SET EXPRESSION drops the altered columns' statistics (only theirs), and autovacuum would
    # re-analyze only after ~10% churn.
    op.execute("ANALYZE public.chunks (fts_en, fts_fr)")


def downgrade() -> None:
    _set_expressions(PLAIN_EXPRESSIONS)
    op.execute("ANALYZE public.chunks (fts_en, fts_fr)")
