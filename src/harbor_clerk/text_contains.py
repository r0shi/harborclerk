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

from sqlalchemy import ColumnElement, and_, func

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


# Postgres ARE: a backslash before a non-alphanumeric character means that character, and before
# an alphanumeric one it is an escape (\m, \d, ...). So only ASCII punctuation is escaped.
_ARE_PUNCTUATION_RE = re.compile(r"""([!"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])""")

# Word characters for the boundary check, the same in every database locale: [[:alnum:]] is ASCII
# only under LC_CTYPE C, so the Latin letters (Latin-1 letters, Latin Extended, IPA, spacing
# modifiers) and the combining marks are listed explicitly. The marks matter in every locale: in
# NFD text "cafe" + U+0301 must not end a word after "cafe".
_EXTRA_WORD_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AA, 0x00AA),  # feminine ordinal
    (0x00B5, 0x00B5),  # micro sign
    (0x00BA, 0x00BA),  # masculine ordinal
    (0x00C0, 0x00D6),  # Latin-1 letters, before the multiplication sign
    (0x00D8, 0x00F6),  # ... between it and the division sign
    (0x00F8, 0x036F),  # the rest of Latin-1, Latin Extended-A/B, IPA, modifiers, combining marks
)
_WORD_CLASS = (
    "[[:alnum:]_"
    + "".join(f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in _EXTRA_WORD_RANGES)
    + "]"
)

# "Not glued to a word character on this side." For a value whose edge character is a word
# character this is \m / \M with a locale-independent notion of word; unlike them it also works
# when the edge is punctuation ("$5").
_WORD_START = f"(?<!{_WORD_CLASS})"
_WORD_END = f"(?!{_WORD_CLASS})"


def escape_are(value: str) -> str:
    """``value`` as a Postgres ARE that matches it literally."""
    return _ARE_PUNCTUATION_RE.sub(r"\\\1", value)


def text_contains_clause(column: ColumnElement[str], value: str) -> ColumnElement[bool]:
    """``column`` contains ``value`` as a literal, case-insensitive, whitespace-folded substring.

    Inner whitespace matches any run of whitespace. Leading or trailing whitespace asks for a word
    boundary on that side: ``" IP "`` matches "the IP clause" and "IP." but not "ship", and a
    trailing newline from the CLI asks only for a word end. The boundary is a regex recheck ANDed
    to the substring match, which stays the indexed predicate.

    The left side must stay exactly ``hc_fold_whitespace(<column>)`` for the planner to use
    ``ix_chunks_chunk_text_ws_trgm``; ``%``, ``_`` and ``\\`` in ``value`` match literally. An
    all-whitespace value folds to "" and matches every chunk.
    """
    folded_column = func.hc_fold_whitespace(column)
    folded = fold_whitespace(value)
    core = folded.strip(" ")
    clause = folded_column.ilike(f"%{escape_ilike(core)}%", escape="\\")
    if not core:
        return clause
    start = _WORD_START if folded.startswith(" ") else ""
    end = _WORD_END if folded.endswith(" ") else ""
    if start or end:
        # lower() on both sides, as ILIKE compares in a UTF-8 database: ~* matches only the pattern
        # character's own case variants, so the Kelvin sign, dotted I, capital sharp s and the Ohm
        # sign would pass the ILIKE and fail the recheck. The core is lowered, not the boundary
        # class (which already lists both cases).
        pattern = func.concat(start, func.lower(escape_are(core)), end)
        clause = and_(clause, func.lower(folded_column).op("~")(pattern))
    return clause
