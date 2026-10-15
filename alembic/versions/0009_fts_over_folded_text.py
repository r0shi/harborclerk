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
FTS GIN indexes included). Both columns change in one ALTER TABLE, so it is one rewrite. It runs
in the migration's transaction: an interrupted upgrade rolls back whole and leaves 0008 stamped,
with nothing half-built for the retry to trip over. Measured on a private PostgreSQL 18.3
database (LC_CTYPE C, M-series Mac) with 40,000 synthetic chunks (840 characters on average,
mixed ASCII spaces, NBSP, U+202F and line breaks, 70% English and 30% French, every chunk
embedded): the upgrade took 26-27 s and held ACCESS EXCLUSIVE on ``chunks`` for 25-26 s of it;
the downgrade 25 s, lock 24 s. About 16 s of that is rebuilding the HNSW index (REINDEX of it
alone: 16 s; without it the upgrade took 11 s). The time grows with the chunk count: the CUAD
sample's 6,408 chunks are a few seconds. The rewrite also rebuilds an INVALID index left by an
interrupted concurrent build (making it valid); 0008's leftovers are built from their real
definitions, so that is harmless.

While it runs, nothing else can read or write ``chunks``. The Mac app runs migrations after
PostgreSQL starts and before the API and workers (``ServiceManager.startAll`` ->
``MigrationRunner``), so nothing is waiting: the app is simply slower to come up once. In Compose
the API container migrates before it serves, and the workers wait on its health check. The
"Run Migrations" maintenance action runs alembic beside a live API with a 120 s limit; a search
during the rewrite waits for it, and a run killed at the limit rolls back to 0008 (run it again,
or restart).

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
    """0008's guard, not a copy of it: the function exists with 0008's exact definition.

    Its comparison twin gets a name of its own here. The guard casts the twin's name with
    ``'...'::regprocedure``, which is resolved to an OID when the statement is prepared, and asyncpg
    caches prepared statements per connection: when 0008 has just run the same check in the same
    session (it does when it found the function already there), its cached statement still
    carries the OID of 0008's twin, dropped since, the comparison finds no row, and the guard
    refuses a function that is in fact identical.
    """
    path = Path(__file__).with_name("0008_chunk_text_ws_fold.py")
    spec = importlib.util.spec_from_file_location("_hc_migration_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._EXPECTED_FUNCTION = "pg_temp.hc_fold_whitespace_expected_0009"
    module._create_fold_function(op.get_bind())


def _set_expressions(expressions: dict[str, str]) -> None:
    clauses = ", ".join(f"ALTER COLUMN {column} SET EXPRESSION AS ({expr})" for column, expr in expressions.items())
    op.execute(f"ALTER TABLE public.chunks {clauses}")


def upgrade() -> None:
    _require_fold_function()
    _set_expressions(FOLDED_EXPRESSIONS)


def downgrade() -> None:
    _set_expressions(PLAIN_EXPRESSIONS)
