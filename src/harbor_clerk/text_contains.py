"""The ``text_contains`` filter: a case-insensitive literal substring of chunk text, whitespace-folded.

Every surface that takes ``text_contains`` (REST search and find-all, the ``kb_search`` and
``kb_find_all`` MCP tools, the chat ``find_all_documents`` tool, the CLI) reaches
``search.hybrid_search``, which builds its predicate here and nowhere else.

The stored text keeps whatever whitespace extraction produced: PDF text puts non-breaking spaces
and line breaks inside phrases, so "right of first refusal" was stored as
``right\\u00a0of\\u00a0first\\u00a0refusal`` and ``first\\nopportunity``, and a plain ``ILIKE`` on
the phrase never matched them (#819). So both sides are folded the same way: every run of
whitespace becomes one ASCII space. The column side is the SQL function
``public.hc_fold_whitespace(text)`` (migration ``0008_chunk_text_ws_fold``), which a GIN trigram
expression index covers, so the match stays an index scan. The argument side is
``fold_whitespace`` below. Case is left to ``ILIKE``; punctuation and hyphens are not touched.

Whitespace means the Unicode ``White_Space`` property: Python's ``str.isspace`` minus U+001C..U+001F
(the information separators, which Python counts and Unicode does not). The SQL function's
character class is frozen in the migration; ``tests/test_text_contains_whitespace.py`` holds it to
``WHITESPACE_RANGES`` against the live function, character by character.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, func

from harbor_clerk.sql_escape import escape_ilike

# Inclusive code point ranges of the Unicode White_Space property.
WHITESPACE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0009, 0x000D),  # tab, LF, VT, FF, CR
    (0x0020, 0x0020),  # space
    (0x0085, 0x0085),  # next line
    (0x00A0, 0x00A0),  # no-break space
    (0x1680, 0x1680),  # ogham space mark
    (0x2000, 0x200A),  # en quad .. hair space, including U+2007 figure space
    (0x2028, 0x2029),  # line and paragraph separators
    (0x202F, 0x202F),  # narrow no-break space
    (0x205F, 0x205F),  # medium mathematical space
    (0x3000, 0x3000),  # ideographic space
)


def _whitespace_run_pattern() -> str:
    parts = [f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in WHITESPACE_RANGES]
    return "[" + "".join(parts) + "]+"


# One run of whitespace, spelled with \uXXXX escapes, which Python's ``re`` and Postgres AREs both
# read the same way: the migration's function body uses this exact string.
WHITESPACE_RUN_PATTERN = _whitespace_run_pattern()
_WHITESPACE_RUN_RE = re.compile(WHITESPACE_RUN_PATTERN)


def fold_whitespace(value: str) -> str:
    """Replace every run of Unicode whitespace in ``value`` with one ASCII space."""
    return _WHITESPACE_RUN_RE.sub(" ", value)


def text_contains_clause(column: ColumnElement[str], value: str) -> ColumnElement[bool]:
    """``column`` contains ``value`` as a literal, case-insensitive, whitespace-folded substring.

    The left side must stay exactly ``hc_fold_whitespace(<column>)`` for the planner to use
    ``ix_chunks_chunk_text_ws_trgm``; ``%``, ``_`` and ``\\`` in ``value`` match literally.
    """
    pattern = f"%{escape_ilike(fold_whitespace(value))}%"
    return func.hc_fold_whitespace(column).ilike(pattern, escape="\\")
