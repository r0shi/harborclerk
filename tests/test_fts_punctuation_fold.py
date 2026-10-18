"""Keyword search and text_contains read punctuation-folded chunk text (0011, #828).

Under LC_CTYPE C (the Mac app's cluster) and the builtin C.UTF-8 provider the text search parser
reads every non-ASCII character as a letter, so "the Party<U+2019>s obligations" indexed
``'party<U+2019>'``, "first<U+2013>refusal" one lexeme, French "l<U+2019>accord" one lexeme, and a
search for "party obligations", "first refusal" or "accord" missed them. The zero-width space glues
words in every locale, libc en_US.UTF-8 included. ``fts_en`` / ``fts_fr`` now index
``hc_fold_search_text(chunk_text)``, the query is folded the same way (``search.websearch_tsquery``),
and ``text_contains`` compares the same folded text, so a typed apostrophe finds a curly one.

Each test runs in three private databases (#827's locale fixtures: libc C, builtin C.UTF-8, libc
en_US.UTF-8, the last skipped where the server lacks it) and asserts its locale's premise. The
embedder is down in every search here, so the FTS leg is the only source of a hit.
"""

import pytest
from sqlalchemy import Text, cast, select, text
from sqlalchemy.exc import DBAPIError

import harbor_clerk.search as search_mod
from harbor_clerk.mcp_lookup_tools import _query_documents_by_date
from tests.test_fts_folded_text import EN_US_LOCALE_SQL, _alembic
from tests.test_text_contains_locales import (
    C_LOCALE_SQL,
    UTF8_LOCALE_SQL,
    _embedder_down,
    _private_database,
    _seed,
    _session,
)

APOSTROPHE = chr(0x2019)
LEFT_QUOTE, RIGHT_QUOTE = chr(0x201C), chr(0x201D)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
HYPHEN = chr(0x2010)
MINUS = chr(0x2212)
ZWSP = chr(0x200B)
SOFT_HYPHEN = chr(0x00AD)

PRE_REVISION = "0010_chat_message_seq"

# label -> (chunk text, the query that must find it, the query's FTS config)
CASES = {
    "possessive": (f"The Party{APOSTROPHE}s obligations survive termination.", "party obligations", "english"),
    "en-dash": (f"The distributor holds a first{EN_DASH}refusal over new territories.", "first refusal", "english"),
    "zwsp": (f"The distributor holds a first{ZWSP}refusal over new territories.", "first refusal", "english"),
    "soft-hyphen": (f"The con{SOFT_HYPHEN}tract renews each year.", "contract renews", "english"),
    "quoted": (f"The {LEFT_QUOTE}Agreement{RIGHT_QUOTE} means this document.", "agreement means", "english"),
    "french": (f"Les parties signent l{APOSTROPHE}accord de distribution.", "accord distribution", "french"),
}
OTHER = "The licensee holds an option over new territories."
TEXTS = {**{label: chunk for label, (chunk, _, _) in CASES.items()}, "other": OTHER}

# Which cases the unfolded columns (0010) find, per locale: under C ctype none; en_US.UTF-8 parses
# the typographic apostrophe, the en dash and the quotes as separators, never the zero-width
# characters.
UNFOLDED_FINDS = {
    "c": set(),
    "utf8": set(),
    "en_us": {"possessive", "en-dash", "quoted", "french"},
}


@pytest.fixture(scope="module")
def punct_c_url():
    yield from _private_database("punct_c", C_LOCALE_SQL)


@pytest.fixture(scope="module")
def punct_utf8_url():
    yield from _private_database("punct_utf8", UTF8_LOCALE_SQL)


@pytest.fixture(scope="module")
def punct_en_us_url():
    databases = _private_database("punct_en_us", EN_US_LOCALE_SQL)
    url = None
    try:
        url = next(databases)
    except DBAPIError as exc:
        if "locale" not in str(exc).lower():
            raise
        pytest.skip(f"the server has no libc en_US.UTF-8 locale: {str(exc).splitlines()[0]}")
    try:
        yield url
    finally:
        databases.close()  # drops the database


@pytest.fixture(params=["c", "utf8", "en_us"])
def locale(request):
    """(name, url) of a private database in that locale."""
    return request.param, request.getfixturevalue(f"punct_{request.param}_url")


async def _assert_locale_premise(session, name: str) -> None:
    ctype = (
        await session.execute(text("SELECT datctype FROM pg_database WHERE datname = current_database()"))
    ).scalar()
    assert ctype == ("en_US.UTF-8" if name == "en_us" else "C")
    tsv = (await session.execute(text("SELECT to_tsvector('english', :t)::text"), {"t": TEXTS["possessive"]})).scalar()
    if name == "en_us":
        assert "'parti'" in tsv, f"premise: en_US.UTF-8 parses the curly apostrophe as a separator; got {tsv}"
    else:
        assert "'parti'" not in tsv, f"premise: C ctype reads the curly apostrophe as a letter; got {tsv}"
    tsv = (await session.execute(text("SELECT to_tsvector('english', :t)::text"), {"t": TEXTS["zwsp"]})).scalar()
    assert "'refus'" not in tsv, f"premise: every locale reads the zero-width space as a letter; got {tsv}"


async def _fts_finds(session, ids: dict[str, str]) -> set[str]:
    """Labels whose chunk the case's own query finds in its config's column (no Python fold here)."""
    found = set()
    for label, (_, query, config) in CASES.items():
        column = "fts_en" if config == "english" else "fts_fr"
        rows = await session.execute(
            text(f"SELECT doc_id::text FROM chunks WHERE {column} @@ websearch_to_tsquery('{config}', :q)"),
            {"q": query},
        )
        if ids[label] in {r[0] for r in rows}:
            found.add(label)
    return found


async def _analyzed(session) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT attname FROM pg_stats WHERE schemaname = 'public' AND tablename = 'chunks' "
            "AND attname IN ('fts_en', 'fts_fr')"
        )
    )
    return {r[0] for r in rows}


async def test_migration_recomputes_existing_rows_over_the_punctuation_fold(locale):
    """Rows seeded at 0010: the unfolded-punctuation columns miss what the locale glues. Upgrading to
    0011 recomputes the stored columns of the rows already there, in every locale, and re-analyzes;
    downgrading recomputes them back."""
    name, url = locale
    _alembic(url, "downgrade", PRE_REVISION)
    try:
        async with _session(url) as session:
            await _assert_locale_premise(session, name)
            ids = await _seed(session, TEXTS)
            await session.commit()
            assert await _fts_finds(session, ids) == UNFOLDED_FINDS[name]
            await session.commit()  # no open transaction: the upgrade takes ACCESS EXCLUSIVE on chunks

            _alembic(url, "upgrade", "head")

            assert await _fts_finds(session, ids) == set(CASES)
            assert await _analyzed(session) == {"fts_en", "fts_fr"}
            await session.commit()

            _alembic(url, "downgrade", PRE_REVISION)

            assert await _fts_finds(session, ids) == UNFOLDED_FINDS[name]
            assert await _analyzed(session) == {"fts_en", "fts_fr"}
            await session.commit()
    finally:
        _alembic(url, "upgrade", "head")


async def test_hybrid_search_finds_punctuation_joined_words_through_fts(locale, monkeypatch):
    """New rows at head, and hybrid_search with the embedder down: the FTS leg alone finds each."""
    name, url = locale
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(url) as session:
        await _assert_locale_premise(session, name)
        ids = await _seed(session, TEXTS)
        for label, (_, query, _) in CASES.items():
            result = await search_mod.hybrid_search(session, query, k=20, bypass_reranker=True)
            assert ids[label] in {h.doc_id for h in result.hits}, (label, query)
            assert ids["other"] not in {h.doc_id for h in result.hits}, (label, query)


async def test_a_query_with_typographic_punctuation_is_folded(locale, monkeypatch):
    """The query side, in hybrid_search and kb_documents_by_date's FTS filter: a pasted curly
    apostrophe parses as the typed one, a pasted en dash joins nothing, and a spaced em dash (an
    LLM's favourite) is never negation of the word after it."""
    name, url = locale
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(url) as session:
        ids = await _seed(
            session,
            {
                "straight": "The Party's obligations survive termination.",
                "curly": TEXTS["possessive"],
                "spaced": "The distributor holds a first refusal over new territories.",
                "notice": "Termination requires ninety days of written notice.",
                "other": OTHER,
            },
        )
        cases = {
            f"Party{APOSTROPHE}s obligations": {"straight", "curly"},
            f"first{EN_DASH}refusal": {"spaced"},
            f"termination {EM_DASH} notice": {"notice"},
        }
        for query, expected in cases.items():
            result = await search_mod.hybrid_search(session, query, k=20, bypass_reranker=True)
            assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(query)
            rows = await _query_documents_by_date(session, query=query, limit=20)
            assert {str(doc.doc_id) for doc, _, _ in rows} == {ids[label] for label in expected}, repr(query)
        if name != "en_us":
            # Premise: unfolded, under C ctype, the pasted query is not the typed one.
            unfolded = (
                await session.execute(
                    text("SELECT websearch_to_tsquery('english', :q)::text"), {"q": f"Party{APOSTROPHE}s obligations"}
                )
            ).scalar()
            assert "'parti'" not in unfolded, unfolded


async def _tsquery_text(session, query: str) -> str:
    """The tsquery ``search.websearch_tsquery`` builds for ``query`` (english), as text."""
    return (await session.execute(select(cast(search_mod.websearch_tsquery("english", query), Text)))).scalar()


async def test_a_minus_or_a_hyphen_separates_and_never_negates(locale, monkeypatch):
    """U+2212 minus and U+2010 hyphen parse as en_US.UTF-8 parses them unfolded: as separators. So
    "\u22125 degrees" indexes '5' (never '-5') and is found by "5 degrees", "2019\u20102020" indexes
    '2020' (never '-2020') and is found by "2020", and a pasted "temperature \u22125" is the words, not
    temperature without 5 (which would find the 20-degree chunk instead)."""
    _, url = locale
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(url) as session:
        ids = await _seed(
            session,
            {
                "minus": f"The pipe holds at a temperature of {MINUS}5 degrees.",
                "years": f"The licence runs for the years 2019{HYPHEN}2020 inclusive.",
                "warm": "The pipe holds at a temperature of 20 degrees.",
                "other": OTHER,
            },
        )
        stored = dict(
            (
                await session.execute(
                    text("SELECT d.title, c.fts_en::text FROM chunks c JOIN documents d USING (doc_id)")
                )
            )
            .tuples()
            .all()
        )
        assert "'5'" in stored["minus"] and "'-5'" not in stored["minus"], stored["minus"]
        assert "'2020'" in stored["years"] and "'-2020'" not in stored["years"], stored["years"]

        query = f"temperature {MINUS}5"
        tsquery = await _tsquery_text(session, query)
        assert "!" not in tsquery and "'5'" in tsquery, tsquery
        cases = {"5 degrees": {"minus"}, "2020": {"years"}, query: {"minus"}}
        for q, expected in cases.items():
            result = await search_mod.hybrid_search(session, q, k=20, bypass_reranker=True)
            assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(q)
            rows = await _query_documents_by_date(session, query=q, limit=20)
            assert {str(doc.doc_id) for doc, _, _ in rows} == {ids[label] for label in expected}, repr(q)


async def test_an_unbalanced_curly_quote_is_not_a_phrase(locale, monkeypatch):
    """A pasted \u201c with no closing quote: folded to a space, as en_US.UTF-8 parses it, never to the
    websearch phrase quote, so the words are found apart and in any order."""
    _, url = locale
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(url) as session:
        ids = await _seed(session, {"apart": "The obligations of each Party survive termination.", "other": OTHER})
        for query in (f"{LEFT_QUOTE}Party obligations", f"{chr(0xAB)} Party obligations"):
            tsquery = await _tsquery_text(session, query)
            assert "<->" not in tsquery and "'parti'" in tsquery, (query, tsquery)
            result = await search_mod.hybrid_search(session, query, k=20, bypass_reranker=True)
            assert {h.doc_id for h in result.hits} == {ids["apart"]}, repr(query)


async def test_text_contains_folds_punctuation(locale, monkeypatch):
    """A typed apostrophe finds a curly one and the reverse; a dash or a zero-width space matches a
    space; a soft hyphen is invisible. An ASCII hyphen is still not a space."""
    _, url = locale
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(url) as session:
        ids = await _seed(
            session,
            {
                "straight": "The covenant binds the Party's obligations.",
                "curly": f"The covenant binds the Party{APOSTROPHE}s obligations.",
                "en-dash": f"The covenant grants a first{EN_DASH}refusal right.",
                "zwsp": f"The covenant grants a first{ZWSP}refusal right.",
                "hyphen": "The covenant grants a first-refusal right.",
                "unicode-hyphen": f"The covenant grants a first{HYPHEN}refusal right.",
                "soft-hyphen": f"The covenant con{SOFT_HYPHEN}tract renews.",
            },
        )
        cases = {
            "party's obligations": {"straight", "curly"},
            f"PARTY{APOSTROPHE}S": {"straight", "curly"},
            "first refusal": {"en-dash", "zwsp", "unicode-hyphen"},
            f"first{EM_DASH}refusal": {"en-dash", "zwsp", "unicode-hyphen"},
            "first-refusal": {"hyphen"},
            " contract ": {"soft-hyphen"},
        }
        for phrase, expected in cases.items():
            result = await search_mod.find_all(session, "covenant", max_results=20, text_contains=phrase)
            assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(phrase)
