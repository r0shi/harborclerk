"""keyword search and text_contains over punctuation-folded chunk text

Under LC_CTYPE C (the Mac app's cluster, ``initdb --locale=C``, and the builtin C.UTF-8 provider,
which parses with C ctype too) the text search parser reads every non-ASCII character as a letter.
0008/0009 (#819, #825) fold non-ASCII whitespace before ``to_tsvector`` and in ``text_contains``;
typographic punctuation, which PDF extraction and word processors produce constantly, still glued
words: "the Party\u2019s obligations" indexed ``'party\u2019'``, "first\u2013refusal" one lexeme
``'first\u2013refus'``, "l\u2019accord" ``'l\u2019accord'``, "\u201cAgreement\u201d"
``'\u201cagreement\u201d'``, so a search for "party obligations", "first refusal", "accord" or
"agreement" missed them on the Mac app while Docker (libc en_US.UTF-8) found them (#828). The zero
width characters glue words in every locale, en_US.UTF-8 included. And ``text_contains`` "Party's"
missed "Party\u2019s" everywhere.

``public.hc_fold_search_text(text)`` does what ``hc_fold_whitespace`` does, after first:

- mapping the typographic apostrophes and single quotes (U+2018, U+2019, U+201A, U+201B, U+02BC,
  U+2039, U+203A) to ``'`` and the ellipsis (U+2026) to ``...``;
- deleting the invisible in-word format characters: soft hyphen (U+00AD), zero-width non-joiner
  and joiner (U+200C, U+200D), word joiner (U+2060), BOM (U+FEFF);

and its whitespace class also takes the word separators that are not White_Space: the zero-width
space (U+200B), every non-ASCII hyphen, dash and minus (U+2010..U+2015, U+2212), and the double
quotes and guillemets (U+201C..U+201F, U+00AB, U+00BB). en_US.UTF-8 parses all of these as
separators, so the Mac app now parses them as Docker does and Docker's search is unchanged. None
becomes ASCII ``-`` or ``"``: a ``-`` would change search there (``2019\u20132020`` or ``\u22125``
would index ``'-2020'`` or ``'-5'``, not ``'2020'`` or ``'5'``, and in a query a spaced dash or a
minus would become websearch negation), and a ``"`` would turn a pasted, unbalanced curly quote
into a phrase query. Its arguments are frozen below; the application's mirror is
``harbor_clerk.text_contains.fold_search_text``, held to these strings and to the live function by
``tests/test_search_fold.py``. It is IMMUTABLE (translate, replace and regexp_replace over explicit
code points, no case-insensitive flag, do not depend on locale), a plain SQL function with no SET
clause so the planner inlines it, created only if absent, and an existing one with a different
definition is an error, never replaced (0008's guard, for this name).

A changed fold is a new function, never a redefinition (alembic/AGENTS.md): the stored FTS columns
and the trigram index hold values computed with the body of the day. So, in one transaction:

1. lock ``chunks`` (ACCESS EXCLUSIVE, waiting at most 10 s: ``lock_timeout``, as 0009);
2. drop ``ix_chunks_chunk_text_ws_trgm`` (0008), so the rewrite does not rebuild it;
3. reset ``fts_en`` / ``fts_fr`` over ``hc_fold_search_text`` (``SET EXPRESSION``: one rewrite of
   ``chunks``, which rebuilds every remaining index, the HNSW one included);
4. build ``ix_chunks_chunk_text_fold_trgm`` on ``hc_fold_search_text(chunk_text)``, the index
   ``text_contains`` now reads;
5. drop ``hc_fold_whitespace``: nothing depends on it any more, and a fold left lying around is one
   a future reader may use and silently disagree with the columns (plain DROP, so anything
   unexpected still depending on it fails the migration rather than being dropped with it);
6. ``ANALYZE public.chunks``: SET EXPRESSION drops the two columns' statistics and the new index
   has none (expression statistics are gathered only by an ANALYZE without a column list).

The index is built in the transaction, not CONCURRENTLY: the rewrite holds ACCESS EXCLUSIVE on
``chunks`` anyway, so a concurrent build (0008's) would let nothing through that is not already
blocked, and would be rebuilt by the rewrite after it; in the transaction, an interrupted run rolls
back whole, with no INVALID leftover for a retry to trip over. Dropping the old index first keeps
the lock about as long as 0009's: one trigram build, the same as the rewrite would have done.

The lock, the rewrite, the disk headroom it needs (a second copy of ``chunks`` and its indexes),
the serialised runs (env.py's advisory lock) and the cancellation by ``application_name`` are as
0009 describes.

Measured on a private PostgreSQL 18.3 database (LC_CTYPE C, M-series Mac) with 40,000 synthetic
chunks (844 characters on average, 70% English and 30% French, mixed ASCII spaces, NBSP, U+202F,
line breaks and the punctuation above, every chunk embedded): the upgrade took 38.6-39.7 s and
held ACCESS EXCLUSIVE on ``chunks`` for 37.6-38.8 s of it; the downgrade 36-39 s, lock 35-38 s. On
the same database and machine, at the same time, 0009's upgrade took 38.8-39.7 s. Of the upgrade,
the rewrite is about 37 s (rebuilding the HNSW index alone takes 24 s; the new fold costs 1.9 s per
pass over the table against 0.6 s for the whitespace fold, and the rewrite runs it once per
column), the trigram build 2.2 s and the ANALYZE 1.5 s. The time grows with the chunk count. The
Run Migrations route's 120 s limit fits roughly 120,000 chunks; above that, restart the app (or
the API container) to run it at startup.

Downgrade reverses it in one transaction: recreates ``hc_fold_whitespace`` with 0008's definition
(0008's guard), resets the columns to 0009's expressions (the same rewrite), rebuilds
``ix_chunks_chunk_text_ws_trgm`` and drops the new index and function.

Revision ID: 0011_punctuation_fold
Revises: 0010_chat_message_seq
Create Date: 2026-10-18
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0011_punctuation_fold"
down_revision: str | None = "0010_chat_message_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

# The function's arguments. Frozen: never edit (a changed fold is a new function in a new migration).
# translate() maps each character of TRANSLATE_FROM to the one at the same position in TRANSLATE_TO and
# deletes those past its end (the last five).
TRANSLATE_FROM = (
    "\u2018\u2019\u201a\u201b\u02bc\u2039\u203a"  # -> '
    "\u00ad\u200c\u200d\u2060\ufeff"  # deleted
)
TRANSLATE_TO = "'''''''"
EXPANSIONS = {"\u2026": "..."}
# One run of White_Space or a word separator, as a Postgres ARE with backslash-u escapes.
RUN_PATTERN = (
    r"[\u0009-\u000d\u0020\u0085\u00a0\u00ab\u00bb\u1680\u2000-\u200b\u2010-\u2015\u201c-\u201f"
    r"\u2028-\u2029\u202f\u205f\u2212\u3000]+"
)

FOLD_FUNCTION = "public.hc_fold_search_text"
_EXPECTED_FUNCTION = "pg_temp.hc_fold_search_text_expected"
FOLD_INDEX = "ix_chunks_chunk_text_fold_trgm"

# 0008's function and index, and 0009's expressions, which the downgrade restores.
OLD_FUNCTION = "public.hc_fold_whitespace"
OLD_INDEX = "ix_chunks_chunk_text_ws_trgm"

# The model's Computed(...) expressions (models/chunk.py) are these, verbatim;
# tests/test_fts_folded_text.py holds the live columns to the model.
FOLDED_EXPRESSIONS = {
    "fts_en": "to_tsvector('english', public.hc_fold_search_text(coalesce(chunk_text, '')))",
    "fts_fr": "to_tsvector('french', public.hc_fold_search_text(coalesce(chunk_text, '')))",
}
# 0009's FOLDED_EXPRESSIONS (a test holds the two equal).
PREVIOUS_EXPRESSIONS = {
    "fts_en": "to_tsvector('english', public.hc_fold_whitespace(coalesce(chunk_text, '')))",
    "fts_fr": "to_tsvector('french', public.hc_fold_whitespace(coalesce(chunk_text, '')))",
}

LOCK_TIMEOUT = "10s"
LOCK_NOT_AVAILABLE = "55P03"


def _e_literal(value: str) -> str:
    """``value`` as a Postgres escape string constant, ASCII only (non-ASCII as backslash-u)."""
    out = []
    for ch in value:
        if ch in "'\\":
            out.append("\\" + ch)
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            assert ord(ch) <= 0xFFFF, ch
            out.append(f"\\u{ord(ch):04x}")
    return "E'" + "".join(out) + "'"


def function_ddl(name: str) -> str:
    """The CREATE FUNCTION statement for ``name`` (also used, by the guard, for a pg_temp twin)."""
    expr = f"pg_catalog.translate($1, {_e_literal(TRANSLATE_FROM)}, {_e_literal(TRANSLATE_TO)})"
    for char, expansion in EXPANSIONS.items():
        expr = f"pg_catalog.replace({expr}, {_e_literal(char)}, {_e_literal(expansion)})"
    return (
        f"CREATE FUNCTION {name}(text) RETURNS text "
        "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE "
        f"RETURN pg_catalog.regexp_replace({expr}, '{RUN_PATTERN}', ' ', 'g')"
    )


_FOLD_INDEX_DDL = (
    f"CREATE INDEX {FOLD_INDEX} ON public.chunks USING gin ({FOLD_FUNCTION}(chunk_text) public.gin_trgm_ops)"
)
# 0008's index definition, built here in the transaction rather than concurrently.
_OLD_INDEX_DDL = f"CREATE INDEX {OLD_INDEX} ON public.chunks USING gin ({OLD_FUNCTION}(chunk_text) public.gin_trgm_ops)"


def _create_fold_function(bind) -> None:
    """Create the function if absent; if present, require exactly this definition, and refuse to touch
    one that differs. 0008's guard (``_create_fold_function``), for this function: see its comments."""
    if bind.execute(sa.text(f"SELECT to_regprocedure('{FOLD_FUNCTION}(text)')")).scalar() is None:
        bind.execute(sa.text(function_ddl(FOLD_FUNCTION)))
        return
    bind.execute(sa.text(function_ddl(_EXPECTED_FUNCTION)))
    same = bind.execute(
        sa.text(
            "SELECT pg_get_function_sqlbody(a.oid) IS NOT DISTINCT FROM pg_get_function_sqlbody(b.oid) "
            "AND a.provolatile = b.provolatile AND a.proisstrict = b.proisstrict "
            "AND a.proparallel = b.proparallel AND a.prosecdef = b.prosecdef "
            "AND a.proconfig IS NOT DISTINCT FROM b.proconfig "
            "AND a.prorettype = b.prorettype "
            "FROM pg_proc a, pg_proc b WHERE a.oid = to_regprocedure(:actual) AND b.oid = to_regprocedure(:expected)"
        ),
        {"actual": f"{FOLD_FUNCTION}(text)", "expected": f"{_EXPECTED_FUNCTION}(text)"},
    ).scalar()
    bind.execute(sa.text(f"DROP FUNCTION {_EXPECTED_FUNCTION}(text)"))
    if not same:
        raise RuntimeError(
            f"{FOLD_FUNCTION}(text) exists with a different definition; an expression index built on it "
            "would be stale. A changed fold is a new function name in a new migration (alembic/AGENTS.md)."
        )


def _create_old_fold_function() -> None:
    """0008's guard, not a copy of it: ``hc_fold_whitespace`` exists with 0008's exact definition."""
    path = Path(__file__).with_name("0008_chunk_text_ws_fold.py")
    spec = importlib.util.spec_from_file_location("_hc_migration_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._create_fold_function(op.get_bind())


def _lock_chunks() -> None:
    """ACCESS EXCLUSIVE on chunks, waiting at most LOCK_TIMEOUT: at startup nothing holds it; beside a live
    API (the Run Migrations route) a long wait would queue every search behind this one (0009's reasoning)."""
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    try:
        op.execute("LOCK TABLE public.chunks IN ACCESS EXCLUSIVE MODE")
    except sa.exc.DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"0011 could not lock public.chunks within {LOCK_TIMEOUT}: another transaction holds it "
            "(a search or a worker on a running API). Retry when it is idle, or restart the app (or the API "
            "container) so the migration runs at startup, before anything else opens the table."
        ) from exc
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def _set_expressions(expressions: dict[str, str]) -> None:
    clauses = ", ".join(f"ALTER COLUMN {column} SET EXPRESSION AS ({expr})" for column, expr in expressions.items())
    op.execute(f"ALTER TABLE public.chunks {clauses}")


def upgrade() -> None:
    _create_fold_function(op.get_bind())
    _lock_chunks()
    op.execute(f"DROP INDEX IF EXISTS public.{OLD_INDEX}")
    _set_expressions(FOLDED_EXPRESSIONS)
    op.execute(_FOLD_INDEX_DDL)
    op.execute(f"DROP FUNCTION {OLD_FUNCTION}(text)")
    op.execute("ANALYZE public.chunks")


def downgrade() -> None:
    _create_old_fold_function()
    _lock_chunks()
    op.execute(f"DROP INDEX IF EXISTS public.{FOLD_INDEX}")
    _set_expressions(PREVIOUS_EXPRESSIONS)
    op.execute(_OLD_INDEX_DDL)
    op.execute(f"DROP FUNCTION {FOLD_FUNCTION}(text)")
    op.execute("ANALYZE public.chunks")
