"""text_contains ignores differences in whitespace (#819).

PDF extraction leaves non-breaking spaces and line breaks inside phrases, so a literal
``ILIKE '%first refusal%'`` missed ``first\\u00a0refusal`` and ``first\\nopportunity``. Both sides are
now folded: every run of Unicode whitespace becomes one ASCII space (``fold_search_text``, which
also folds typographic punctuation since #828: tests/test_fts_punctuation_fold.py).
"""

import pytest
from sqlalchemy import select, text

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import find_all, hybrid_search
from harbor_clerk.text_contains import (
    WHITESPACE_RANGES,
    escape_are,
    fold_search_text,
    text_contains_clause,
)

# Each chunk sits in its own document; every chunk shares the word "covenant" so a query for it
# makes every document a candidate, and the filter alone decides what survives.
MATCHING = {
    "plain": "The covenant grants a first refusal to the buyer.",
    "newline": "The covenant grants a first\nrefusal to the buyer.",
    "space-tab-space": "The covenant grants a first \t refusal to the buyer.",
    "double-space": "The covenant grants a first  refusal to the buyer.",
    "nbsp": "The covenant grants a right\u00a0of\u00a0first\u00a0refusal to the buyer.",
    "narrow-nbsp": "The covenant grants a first\u202frefusal to the buyer.",
    "figure-space": "The covenant grants a first\u2007refusal to the buyer.",
    "crlf": "The covenant grants a first\r\nrefusal to the buyer.",
}
NOT_MATCHING = {
    "joined": "The covenant grants a firstrefusal to the buyer.",
    "hyphen": "The covenant grants a first-refusal to the buyer.",
    "other-words": "The covenant grants a first option to the buyer.",
}


def _whitespace_code_points() -> set[int]:
    return {cp for lo, hi in WHITESPACE_RANGES for cp in range(lo, hi + 1)}


async def _seed(db_session, texts: dict[str, str]) -> dict[str, str]:
    """One active document per text; returns label -> doc_id (str)."""
    ids: dict[str, str] = {}
    for i, (label, chunk_text) in enumerate(texts.items()):
        doc = Document(
            title=label,
            status="active",
            sha256=f"sha_ws_{i:024d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=chunk_text, language="english"))
        ids[label] = str(doc.doc_id)
    await db_session.flush()
    return ids


# ---------------------------------------------------------------------------
# The fold itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("first refusal", "first refusal"),
        ("first\nrefusal", "first refusal"),
        ("first \t refusal", "first refusal"),
        ("first  refusal", "first refusal"),
        ("first\u00a0\u202f\u2007refusal", "first refusal"),
        ("first\r\n\r\nrefusal", "first refusal"),
        ("firstrefusal", "firstrefusal"),
        ("first-refusal", "first-refusal"),
        ("50%_x", "50%_x"),
        ("First Refusal", "First Refusal"),  # case is ILIKE's business, not the fold's
    ],
)
def test_fold_search_text_folds_whitespace(raw, folded):
    assert fold_search_text(raw) == folded


def test_whitespace_set_is_unicode_white_space():
    """Python's isspace minus the information separators U+001C..U+001F is Unicode White_Space."""
    expected = {cp for cp in range(0x110000) if chr(cp).isspace()} - set(range(0x1C, 0x20))
    assert _whitespace_code_points() == expected


# ---------------------------------------------------------------------------
# The SQL function and its index
# ---------------------------------------------------------------------------


async def test_sql_function_folds_every_whitespace_run(db_session):
    """Every White_Space code point folds to one space in Postgres as in Python; the information
    separators do not (the whole fold is held to Python in tests/test_search_fold.py)."""
    for cp in sorted(_whitespace_code_points()):
        value = f"a{chr(cp)}{chr(cp)}b"
        got = (await db_session.execute(text("SELECT hc_fold_search_text(:v)"), {"v": value})).scalar()
        assert got == "a b" == fold_search_text(value), f"U+{cp:04X} not folded on both sides"
    for cp in (0x1C, 0x1F, ord("-"), ord("_")):
        value = f"a{chr(cp)}b"
        got = (await db_session.execute(text("SELECT hc_fold_search_text(:v)"), {"v": value})).scalar()
        assert got == value == fold_search_text(value), f"U+{cp:04X} folded on one side"


@pytest.mark.parametrize("value", ["first refusal", " first refusal\n"])
async def test_helper_predicate_can_use_the_fold_index(db_session, value):
    """The planner matches the helper's left side to the expression index, with the pattern bound as a
    parameter as it is in production (seq scan disabled: the table is tiny), with or without the
    word-boundary recheck that edge whitespace adds."""
    stmt = select(Chunk.chunk_id).where(text_contains_clause(Chunk.chunk_text, value))
    conn = await db_session.connection()
    compiled = stmt.compile(dialect=conn.dialect)
    params = [compiled.params[name] for name in compiled.positiontup]
    # Through the session, so the transaction the raw connection runs EXPLAIN in has begun: on the
    # bare driver connection SET LOCAL outside a transaction is a no-op and the tiny table seq-scans.
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    raw = (await conn.get_raw_connection()).driver_connection
    plan = "\n".join(r[0] for r in await raw.fetch("EXPLAIN " + str(compiled), *params))
    assert "ix_chunks_chunk_text_fold_trgm" in plan, plan
    # The index must serve the folded ILIKE itself, not some other conjunct.
    index_conds = [line for line in plan.splitlines() if "Index Cond" in line]
    assert any("~~*" in line and "regexp_replace" in line for line in index_conds), plan


# ---------------------------------------------------------------------------
# Through hybrid_search and find_all
# ---------------------------------------------------------------------------


async def test_hybrid_search_matches_every_whitespace_variant(db_session):
    ids = await _seed(db_session, {**MATCHING, **NOT_MATCHING})

    unfiltered = await hybrid_search(db_session, "covenant", k=50)
    assert {h.doc_id for h in unfiltered.hits} == set(ids.values()), "every document must be a candidate"

    result = await hybrid_search(db_session, "covenant", k=50, text_contains="first refusal")
    assert {h.doc_id for h in result.hits} == {ids[label] for label in MATCHING}


async def test_find_all_matches_every_whitespace_variant(db_session):
    ids = await _seed(db_session, {**MATCHING, **NOT_MATCHING})

    unfiltered = await find_all(db_session, "covenant", max_results=50)
    assert unfiltered.total_matches == len(ids)

    result = await find_all(db_session, "covenant", max_results=50, text_contains="first refusal")
    assert {h.doc_id for h in result.hits} == {ids[label] for label in MATCHING}
    assert result.total_matches == len(MATCHING)


async def test_argument_whitespace_is_folded_too(db_session):
    """A phrase typed with odd whitespace matches the plainly spaced chunk."""
    ids = await _seed(db_session, {"plain": MATCHING["plain"], "other": NOT_MATCHING["other-words"]})

    for phrase in ("first\u00a0 \nrefusal", "first\trefusal", "first   refusal"):
        result = await find_all(db_session, "covenant", max_results=10, text_contains=phrase)
        assert {h.doc_id for h in result.hits} == {ids["plain"]}, repr(phrase)


async def test_ilike_metacharacters_still_literal(db_session):
    ids = await _seed(
        db_session,
        {
            "underscore": "The covenant grants a first_refusal right.",
            "letter": "The covenant grants a firstxrefusal right.",
            "percent": "The covenant pays 50%\u00a0up front.",
            "digits": "The covenant pays 500 up front.",
        },
    )

    result = await hybrid_search(db_session, "covenant", k=10, text_contains="first_refusal")
    assert {h.doc_id for h in result.hits} == {ids["underscore"]}

    result = await hybrid_search(db_session, "covenant", k=10, text_contains="50% up")
    assert {h.doc_id for h in result.hits} == {ids["percent"]}


async def test_edge_whitespace_asks_for_a_word_boundary(db_session):
    """Stripped for the indexed substring match, but an edge space still means a word edge there:
    " IP " is the word IP, not "ship"; a trailing newline from the CLI asks only for a word end."""
    ids = await _seed(
        db_session,
        {
            "ip": "The covenant covers the IP clause.",
            "ip-dot": "The covenant assigns all IP.",
            "ship": "The covenant covers the ship.",
            "ipo": "The covenant precedes the IPO.",
            # "_" is a word character: these fail if it drops out of the boundary class.
            "under-ip": "The covenant covers _IP terms.",
            "ip-under": "The covenant covers IP_x terms.",
        },
    )
    cases = {
        "IP": {"ip", "ip-dot", "ship", "ipo", "under-ip", "ip-under"},
        " IP ": {"ip", "ip-dot"},
        "\tIP\r\n": {"ip", "ip-dot"},
        " IP": {"ip", "ip-dot", "ipo", "ip-under"},
        "IP ": {"ip", "ip-dot", "ship", "under-ip"},
        "IP\n": {"ip", "ip-dot", "ship", "under-ip"},
    }
    for phrase, expected in cases.items():
        result = await find_all(db_session, "covenant", max_results=10, text_contains=phrase)
        assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(phrase)


async def test_edge_whitespace_with_inner_whitespace_variants(db_session):
    """The boundary recheck runs on the folded column, so it holds across NBSPs and line breaks."""
    ids = await _seed(
        db_session,
        {
            "ends": "The covenant grants a first\nrefusal",
            "plural": "The covenant grants a first refusals list.",
            "prefix": "The covenant grants a cofirst refusal.",
        },
    )
    for phrase, expected in {
        "first refusal\n": {"ends", "prefix"},
        " first refusal": {"ends", "plural"},
        "\tfirst refusal \r\n": {"ends"},
        "first refusal": {"ends", "plural", "prefix"},
    }.items():
        result = await find_all(db_session, "covenant", max_results=10, text_contains=phrase)
        assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(phrase)


async def test_word_boundary_recheck_escapes_regex_metacharacters(db_session):
    """Each case would match differently (or raise) if the value reached the regex unescaped."""
    ids = await _seed(
        db_session,
        {
            "dot": "The covenant uses a.b here.",
            "dot-decoy": "The covenant uses axb and a.bc here.",
            "paren": "The covenant says f(x) holds.",
            "plus": "The covenant targets c++ code.",
            "backslash": "The covenant path is a\\b today.",
            "dollar": "The covenant fee is $5 flat.",
            "dollar-decoy": "The covenant fee is $50 flat.",
        },
    )
    for phrase, expected in {
        " a.b ": {"dot"},
        " f(x) ": {"paren"},
        " c++ ": {"plus"},
        " a\\b ": {"backslash"},
        " $5 ": {"dollar"},
    }.items():
        result = await find_all(db_session, "covenant", max_results=10, text_contains=phrase)
        assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(phrase)


@pytest.mark.parametrize("value", ["a.b", "f(x)", "c++", "a\\b", "$5", "[z]", "^x|y", "a{2}", "?*", "-_#"])
async def test_escape_are_matches_literally_in_postgres(db_session, value):
    matched = (
        await db_session.execute(text("SELECT :v ~ ('^' || :e || '$')"), {"v": value, "e": escape_are(value)})
    ).scalar()
    assert matched is True
