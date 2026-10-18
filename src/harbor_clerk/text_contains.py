"""The ``text_contains`` filter, and the fold that it and keyword search share.

Every surface that takes ``text_contains`` (REST search and find-all, the ``kb_search`` and
``kb_find_all`` MCP tools, the chat ``find_all_documents`` tool, the CLI) reaches
``search.hybrid_search``, which builds its predicate here and nowhere else.

The stored text keeps whatever extraction produced. PDF text puts non-breaking spaces and line
breaks inside phrases (``right\\u00a0of\\u00a0first\\u00a0refusal``, ``first\\nopportunity``,
#819), and word processors write typographic apostrophes, quotes and dashes (``the Party\\u2019s``,
``l\\u2019accord``, ``first\\u2013refusal``, #828). So text is compared folded, the same way on both
sides: ``fold_search_text`` below, and its SQL twin ``public.hc_fold_search_text(text)`` (migration
``0011_punctuation_fold``). A GIN trigram expression index covers the SQL function, so a
``text_contains`` match stays an index scan; ``chunks.fts_en`` / ``fts_fr`` are computed over it,
and ``search.websearch_tsquery`` folds the query with ``fold_search_text``, so keyword search reads
the same folded text on both sides. Case is left to ``ILIKE``.

The fold, in this order:

1. ``FOLD_MAP``: typographic apostrophes and single quotes become ``'``, the ellipsis ``...``;
   the invisible in-word format characters (soft hyphen, zero-width non-joiner and joiner, word
   joiner, BOM) are deleted.
2. Every run of ``RUN_RANGES`` becomes one ASCII space: the Unicode ``White_Space`` property
   (``WHITESPACE_RANGES``: Python's ``str.isspace`` minus U+001C..U+001F, the information
   separators, which Python counts and Unicode does not) and ``SEPARATOR_RANGES``, the characters
   that separate words without being White_Space: the zero-width space, every non-ASCII hyphen,
   dash and minus (U+2010..U+2015, U+2212), and the double quotes and guillemets.

Why: under LC_CTYPE C (the Mac app's cluster, ``initdb --locale=C``) the text search parser reads
every non-ASCII character as a letter, so each of these glued its neighbours into one lexeme
("party\\u2019", "first\\u2013refus"), and ``text_contains`` needed the same character typed. The
apostrophe maps to ``'`` (the point of #828: "Party\\u2019s" is "Party's"). Everything else that
libc en_US.UTF-8 (Docker) parses as a separator folds to a space, so the Mac app parses it as
Docker does and Docker's search does not change. No non-ASCII dash or minus becomes ASCII ``-``:
``2019\\u20132020`` or ``\\u22125`` would index ``-2020`` or ``-5`` (not ``2020``, ``5``), and in a
query a spaced dash or a minus would become websearch negation. No double quote becomes ``"``: a
pasted, unbalanced \\u201c would turn the query into a phrase. The ASCII hyphen-minus is never
touched: in ``text_contains`` "first-refusal" does not match "first refusal".

The SQL function is frozen in the migration; ``tests/test_search_fold.py`` holds it to the tables
here, against the live function, character by character.
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

# Word separators that are not White_Space, folded with it.
SEPARATOR_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AB, 0x00AB),  # left-pointing double angle quotation mark (guillemet)
    (0x00BB, 0x00BB),  # right-pointing double angle quotation mark (guillemet)
    (0x200B, 0x200B),  # zero-width space: a word boundary (UAX #29), unlike the joiners below
    (0x2010, 0x2015),  # hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar
    (0x201C, 0x201F),  # left, right, low-9 and high-reversed-9 double quotation marks
    (0x2212, 0x2212),  # minus sign
)

RUN_RANGES: tuple[tuple[int, int], ...] = WHITESPACE_RANGES + SEPARATOR_RANGES

# Step 1: each character becomes the string it maps to ("" deletes it).
FOLD_MAP: dict[str, str] = {
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark, the typographic apostrophe
    "\u201a": "'",  # single low-9 quotation mark
    "\u201b": "'",  # single high-reversed-9 quotation mark
    "\u02bc": "'",  # modifier letter apostrophe
    "\u2039": "'",  # single left-pointing angle quotation mark
    "\u203a": "'",  # single right-pointing angle quotation mark
    "\u2026": "...",  # horizontal ellipsis
    "\u00ad": "",  # soft hyphen
    "\u200c": "",  # zero-width non-joiner
    "\u200d": "",  # zero-width joiner
    "\u2060": "",  # word joiner
    "\ufeff": "",  # zero-width no-break space (byte order mark)
}


def _bracket_expression(ranges: tuple[tuple[int, int], ...]) -> str:
    """``ranges`` as one bracket expression, adjacent ranges merged, spelled with \\uXXXX escapes,
    which Python's ``re`` and Postgres AREs read the same way."""
    merged: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return "[" + "".join(f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in merged) + "]"


# The SQL function's arguments, which 0011 freezes verbatim: translate() maps each character of
# FOLD_TRANSLATE_FROM to the one at the same position in FOLD_TRANSLATE_TO and deletes those past
# its end, replace() makes each FOLD_EXPANSIONS key its value, and regexp_replace() folds the runs.
FOLD_TRANSLATE_FROM = "".join(k for k, v in FOLD_MAP.items() if len(v) == 1) + "".join(
    k for k, v in FOLD_MAP.items() if not v
)
FOLD_TRANSLATE_TO = "".join(v for v in FOLD_MAP.values() if len(v) == 1)
FOLD_EXPANSIONS: dict[str, str] = {k: v for k, v in FOLD_MAP.items() if len(v) > 1}
FOLD_RUN_PATTERN = _bracket_expression(RUN_RANGES) + "+"

_FOLD_TABLE = str.maketrans(FOLD_MAP)
_RUN_RE = re.compile(FOLD_RUN_PATTERN)
_RUN_CHARS = frozenset(chr(cp) for lo, hi in RUN_RANGES for cp in range(lo, hi + 1))


def fold_search_text(value: str) -> str:
    """``value`` folded as ``public.hc_fold_search_text`` folds it (the module docstring's two steps)."""
    return _RUN_RE.sub(" ", value.translate(_FOLD_TABLE))


def fold_search_text_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    """``fold_search_text(value)``, and for each folded character the span of ``value`` it came from.

    Built one character at a time, so a match found in the folded text can be shown in the original
    (``search._brief_snippet``); a test holds the folded string to ``fold_search_text``'s.
    """
    mapped = [(out, i) for i, ch in enumerate(value) for out in FOLD_MAP.get(ch, ch)]  # step 1
    folded: list[str] = []
    spans: list[tuple[int, int]] = []
    for out, i in mapped:  # step 2; step 1 never produces a space, so a folded space is a run's
        if out in _RUN_CHARS:
            if folded and folded[-1] == " ":
                spans[-1] = (spans[-1][0], i + 1)
                continue
            out = " "
        folded.append(out)
        spans.append((i, i + 1))
    return "".join(folded), spans


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


def text_contains_clause(
    column: ColumnElement[str], value: str, *, prefix_chars: int | None = None
) -> ColumnElement[bool]:
    """``column`` contains ``value`` as a literal, case-insensitive substring, both folded.

    Both sides go through the fold (``fold_search_text``): inner whitespace matches any run of
    whitespace, a typed apostrophe a curly one, and so on. Leading or trailing whitespace asks for a word
    boundary on that side: ``" IP "`` matches "the IP clause" and "IP." but not "ship", and a
    trailing newline from the CLI asks only for a word end. The boundary is a regex recheck ANDed
    to the substring match, which stays the indexed predicate.

    The left side must stay exactly ``hc_fold_search_text(<column>)`` for the planner to use
    ``ix_chunks_chunk_text_fold_trgm``; ``%``, ``_`` and ``\\`` in ``value`` match literally. A value
    that folds to nothing but spaces (all whitespace, a lone dash) matches every chunk.

    ``prefix_chars`` is a capped API key's snippet cap (``passage_cap.snippet_cap``, #831): the value
    must then occur within the column's first ``prefix_chars`` characters, the text the key can
    already read, so the filter answers nothing about text past the cap. The indexed substring match
    over the whole column stays as the prefilter, and the prefix recheck (``hc_fold_search_text`` over
    ``left(column, n)``, the raw prefix folded) is ANDed to it; the word-boundary recheck runs on the
    prefix too, so a boundary at the cap is end of text and the character after it is never consulted:
    a word ending exactly at the cap counts as whole (``" foo "`` matches a chunk whose ``foobar`` is cut
    to ``foo`` by the cap). Deliberate: the answer depends on nothing the key cannot read.
    A cap of 0 (or less) matches nothing but a value that folds to whitespace.
    """
    folded_column = func.hc_fold_search_text(column)
    folded = fold_search_text(value)
    core = folded.strip(" ")
    pattern_ilike = f"%{escape_ilike(core)}%"
    clause = folded_column.ilike(pattern_ilike, escape="\\")
    checked = folded_column  # what the boundary recheck reads
    if prefix_chars is not None:
        checked = func.hc_fold_search_text(func.left(column, max(0, prefix_chars)))
        if core:
            clause = and_(clause, checked.ilike(pattern_ilike, escape="\\"))
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
        clause = and_(clause, func.lower(checked).op("~")(pattern))
    return clause
