"""The search fold (#828): ``text_contains.fold_search_text`` and its SQL twin ``hc_fold_search_text``.

The SQL function is frozen in migration 0011; the Python tables are the application's copy. These
tests hold the migration's strings to the tables, and the live function to ``fold_search_text`` on a
table of edge strings and character by character, so the two never disagree about what a chunk or a
query folds to.
"""

import importlib.util
import random
from pathlib import Path

import pytest
from sqlalchemy import text

from harbor_clerk.search import _brief_snippet
from harbor_clerk.text_contains import (
    FOLD_EXPANSIONS,
    FOLD_MAP,
    FOLD_RUN_PATTERN,
    FOLD_TRANSLATE_FROM,
    FOLD_TRANSLATE_TO,
    RUN_RANGES,
    WHITESPACE_RANGES,
    _bracket_expression,
    fold_search_text,
    fold_search_text_spans,
)

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(f"_mig_{filename[:4]}_fold", _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NBSP = chr(0xA0)
ZWSP = chr(0x200B)
SHY = chr(0xAD)
ZWJ = chr(0x200D)
ZWNJ = chr(0x200C)
BOM = chr(0xFEFF)
WJ = chr(0x2060)
LSQUO, RSQUO, LDQUO, RDQUO = chr(0x2018), chr(0x2019), chr(0x201C), chr(0x201D)
MOD_APOS = chr(0x02BC)
LAQUO, RAQUO = chr(0xAB), chr(0xBB)
HYPHEN, NB_HYPHEN, FIG_DASH, EN_DASH, EM_DASH, BAR, MINUS = (
    chr(0x2010),
    chr(0x2011),
    chr(0x2012),
    chr(0x2013),
    chr(0x2014),
    chr(0x2015),
    chr(0x2212),
)
ELLIPSIS = chr(0x2026)

# (raw, folded): the edge strings both implementations must fold alike.
EDGE_CASES = [
    ("", ""),
    ("plain ascii, 'quoted' \"text\" - x", "plain ascii, 'quoted' \"text\" - x"),
    (f"the Party{RSQUO}s obligations", "the Party's obligations"),
    (f"l{RSQUO}accord d{RSQUO}une", "l'accord d'une"),
    # Double quotes and guillemets separate words; they never become the websearch phrase quote.
    (f"{LSQUO}single{RSQUO} {LDQUO}double{RDQUO}", "'single' double "),
    (f"{LDQUO}Party obligations", " Party obligations"),
    (f"{chr(0x201E)}low{chr(0x201F)}", " low "),
    (f"Hawai{MOD_APOS}i", "Hawai'i"),
    (f"{LAQUO}{NBSP}accord{NBSP}{RAQUO}", " accord "),
    (f"{LAQUO}accord{RAQUO}", " accord "),
    # No non-ASCII hyphen, dash or minus becomes the ASCII hyphen-minus (websearch negation, '-5').
    (f"co{HYPHEN}operate co{NB_HYPHEN}op 5 {MINUS} 3 {MINUS}5", "co operate co op 5 3 5"),
    (f"temperature of {MINUS}5 degrees", "temperature of 5 degrees"),
    (f"2019{HYPHEN}2020", "2019 2020"),
    (f"first{EN_DASH}refusal", "first refusal"),
    (f"2019{EN_DASH}2020 555{FIG_DASH}1234", "2019 2020 555 1234"),
    (f"termination {EM_DASH} notice", "termination notice"),
    (f"rights{EM_DASH}notably{BAR}x", "rights notably x"),
    (f"first{ZWSP}refusal", "first refusal"),
    (f"a {ZWSP} {ZWSP}b", "a b"),
    (f"con{SHY}tract", "contract"),
    (f"zw{ZWJ}j zw{ZWNJ}nj w{WJ}j {BOM}bom", "zwj zwnj wj bom"),
    # Deletion comes before the run fold, so a deleted character between spaces leaves one space.
    (f"a {SHY} b", "a b"),
    (f"a{SHY}{NBSP}{ZWJ}\n{BOM}b", "a b"),
    (f"obligations{ELLIPSIS} and", "obligations... and"),
    (f"{ELLIPSIS}{ELLIPSIS}", "......"),
    (f"first{NBSP}\n\t refusal", "first refusal"),
    ("  edges  ", " edges "),
    (f"{EN_DASH}edge{EM_DASH}", " edge "),
    ("50%_x", "50%_x"),
    ("First Refusal", "First Refusal"),  # case is ILIKE's business, not the fold's
    (f"caf{chr(0xE9)} cafe{chr(0x301)}", f"caf{chr(0xE9)} cafe{chr(0x301)}"),
]

# Characters the fold leaves alone, near misses of the ones it touches.
NEAR_MISSES = [
    0x1C,  # information separators: Python's isspace, not White_Space
    0x1F,
    0x60,  # grave accent (ASCII: parsed alike in every locale)
    0xB4,  # acute accent, sometimes typed for an apostrophe
    0xB7,  # middle dot, inside Catalan words (l\u00b7l)
    0x2016,  # double vertical line, after the horizontal bar
    0x2022,  # bullet
    0x2032,  # prime
    0x2033,  # double prime
    0x2061,  # function application, after the word joiner
    0x180E,  # Mongolian vowel separator, no longer White_Space
    0xFE58,  # small em dash
    0xFF0D,  # fullwidth hyphen-minus
    ord("-"),
    ord("_"),
    ord("'"),
    ord('"'),
]


@pytest.mark.parametrize(("raw", "folded"), EDGE_CASES)
def test_fold_search_text(raw, folded):
    assert fold_search_text(raw) == folded


def _random_strings(count: int = 2000) -> list[str]:
    alphabet = [*" \t\nab'-.", *FOLD_MAP, NBSP, ZWSP, EN_DASH, chr(0x3000)]
    rng = random.Random(828)
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))) for _ in range(count)]


def test_spans_fold_as_fold_search_text_does():
    """The span-tracking fold is the fold, and each span is the original text that folded to it."""
    for value in [raw for raw, _ in EDGE_CASES] + _random_strings():
        folded, spans = fold_search_text_spans(value)
        assert folded == fold_search_text(value), repr(value)
        assert len(spans) == len(folded)
        for ch, (start, end) in zip(folded, spans, strict=True):
            assert 0 <= start < end <= len(value)
            assert ch in fold_search_text(value[start:end]), (value, ch, value[start:end])


def test_migration_freezes_the_python_tables():
    """0011's function arguments are the strings the Python tables build."""
    mig = _load("0011_punctuation_fold.py")
    assert mig.TRANSLATE_FROM == FOLD_TRANSLATE_FROM
    assert mig.TRANSLATE_TO == FOLD_TRANSLATE_TO
    assert mig.EXPANSIONS == FOLD_EXPANSIONS
    assert mig.RUN_PATTERN == FOLD_RUN_PATTERN
    assert len(FOLD_TRANSLATE_FROM) == len(set(FOLD_TRANSLATE_FROM))


def test_0008_froze_the_whitespace_ranges():
    """0008's pattern, which 0011's downgrade recreates, is WHITESPACE_RANGES; the fold extends it."""
    mig = _load("0008_chunk_text_ws_fold.py")
    expected = _bracket_expression(WHITESPACE_RANGES) + "+"
    assert expected == mig.WHITESPACE_RUN_PATTERN
    assert set(WHITESPACE_RANGES) < set(RUN_RANGES)


def test_previous_expressions_are_0009s():
    assert (
        _load("0011_punctuation_fold.py").PREVIOUS_EXPRESSIONS
        == _load("0009_fts_over_folded_text.py").FOLDED_EXPRESSIONS
    )


def test_no_mapped_character_is_also_a_run_character():
    run = {chr(cp) for lo, hi in RUN_RANGES for cp in range(lo, hi + 1)}
    assert not run & set(FOLD_MAP)
    assert not run & set("".join(FOLD_MAP.values()))


# ---------------------------------------------------------------------------
# The live function
# ---------------------------------------------------------------------------


async def test_live_function_is_the_migration_definition(db_session):
    """The function the database runs has 0011's body, as a twin built from 0011's DDL deparses it."""
    mig = _load("0011_punctuation_fold.py")
    await db_session.execute(text(mig.function_ddl("pg_temp.hc_fold_search_text_twin")))
    try:
        same = (
            await db_session.execute(
                text(
                    "SELECT pg_get_function_sqlbody('public.hc_fold_search_text(text)'::regprocedure) = "
                    "pg_get_function_sqlbody('pg_temp.hc_fold_search_text_twin(text)'::regprocedure)"
                )
            )
        ).scalar()
    finally:
        await db_session.rollback()
    assert same is True
    gone = (await db_session.execute(text("SELECT to_regprocedure('public.hc_fold_whitespace(text)')"))).scalar()
    assert gone is None, "0011 drops the whitespace-only fold"


async def _sql_fold(db_session, values: list[str]) -> list[str]:
    rows = await db_session.execute(
        text("SELECT hc_fold_search_text(v) FROM unnest(CAST(:vs AS text[])) WITH ORDINALITY AS t(v, n) ORDER BY n"),
        {"vs": values},
    )
    return [r[0] for r in rows]


async def test_sql_function_agrees_with_python_on_edge_strings(db_session):
    values = [raw for raw, _ in EDGE_CASES] + _random_strings(500)
    assert await _sql_fold(db_session, values) == [fold_search_text(v) for v in values]


async def test_sql_function_agrees_with_python_character_by_character(db_session):
    """Every character the fold touches, and the near misses, alone and doubled between letters."""
    touched = {cp for lo, hi in RUN_RANGES for cp in range(lo, hi + 1)} | {ord(c) for c in FOLD_MAP}
    values = [f"a{chr(cp)}{chr(cp)}b" for cp in sorted(touched | set(NEAR_MISSES))]
    assert await _sql_fold(db_session, values) == [fold_search_text(v) for v in values]
    for cp in NEAR_MISSES:
        assert fold_search_text(f"a{chr(cp)}b") == f"a{chr(cp)}b", f"U+{cp:04X} is folded"


# ---------------------------------------------------------------------------
# find_all's brief snippet centres on a folded match
# ---------------------------------------------------------------------------


def test_brief_snippet_centres_on_a_curly_match_for_a_typed_literal():
    filler = "lorem ipsum dolor sit amet " * 12
    chunk = f"{filler}the Party{RSQUO}s obligations{SHY} survive{NBSP}termination {filler}"
    snippet = _brief_snippet(chunk, "party's obligations survive", max_chars=80)
    assert f"Party{RSQUO}s obligations{SHY} survive" in snippet, snippet
    assert snippet.startswith(ELLIPSIS) and snippet.endswith(ELLIPSIS), snippet


def test_brief_snippet_centres_on_a_dash_matched_by_a_space():
    filler = "lorem ipsum dolor sit amet " * 12
    chunk = f"{filler}a first{EN_DASH}refusal right {filler}"
    snippet = _brief_snippet(chunk, "first refusal", max_chars=60)
    assert f"first{EN_DASH}refusal" in snippet, snippet
