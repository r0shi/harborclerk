"""The full-text columns are computed over whitespace-folded chunk text (0009, #825).

Under LC_CTYPE C (the Mac app's cluster) the text search parser reads a non-breaking space, a
narrow no-break space and the other non-ASCII spaces as letters: "right of first refusal" joined by
NBSPs was one lexeme, ``websearch_to_tsquery('first refusal')`` never matched it, and the chunk
reached hybrid search only through the vector leg. ``fts_en`` / ``fts_fr`` now index
``hc_fold_whitespace(chunk_text)``, and every tsquery against them is built over the folded query.

The locale tests run on #824's private databases (``tests/test_text_contains_locales.py``): one with
libc LC_CTYPE C, one with the builtin C.UTF-8 provider; both parse an NBSP as a letter, which each
test asserts as its premise. The embedder is down in every search here, so the FTS leg is the only
source of a hit.
"""

import os
import subprocess

import pytest
from sqlalchemy import text

import harbor_clerk.search as search_mod
from harbor_clerk.mcp_lookup_tools import _query_documents_by_date
from harbor_clerk.models import Chunk
from tests.test_text_contains_locales import (
    C_LOCALE_SQL,
    UTF8_LOCALE_SQL,
    _embedder_down,
    _private_database,
    _seed,
    _session,
)

NBSP = chr(0xA0)
NARROW_NBSP = chr(0x202F)

EN_TEXT = "The distributor holds a right{s}of{s}first{s}refusal over new territories."
FR_TEXT = "Le distributeur obtient un droit{s}de{s}premier{s}refus sur les nouveaux territoires."
EN_QUERY = "first refusal"
FR_QUERY = "premier refus"
SEPARATORS = {"nbsp": NBSP, "narrow-nbsp": NARROW_NBSP, "newline": "\n"}

TEXTS = {
    **{f"en-{name}": EN_TEXT.format(s=sep) for name, sep in SEPARATORS.items()},
    **{f"fr-{name}": FR_TEXT.format(s=sep) for name, sep in SEPARATORS.items()},
    "other": "The distributor holds an option over new territories.",
}
EN_LABELS = {f"en-{name}" for name in SEPARATORS}
FR_LABELS = {f"fr-{name}" for name in SEPARATORS}
# Before 0009 only the ASCII separator splits words.
UNFOLDED_EN_LABELS = {"en-newline"}
UNFOLDED_FR_LABELS = {"fr-newline"}

PRE_REVISION = "0008_chunk_text_ws_fold"


@pytest.fixture(scope="module")
def fts_c_url():
    yield from _private_database("fts_c", C_LOCALE_SQL)


@pytest.fixture(scope="module")
def fts_utf8_url():
    yield from _private_database("fts_utf8", UTF8_LOCALE_SQL)


@pytest.fixture(params=["c", "utf8"])
def locale_url(request):
    return request.getfixturevalue("fts_c_url" if request.param == "c" else "fts_utf8_url")


def _alembic(url, *args: str) -> None:
    subprocess.run(
        ["alembic", *args],
        check=True,
        env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
    )


async def _assert_nbsp_is_a_letter(session) -> None:
    tsv = (await session.execute(text("SELECT to_tsvector('english', :t)::text"), {"t": TEXTS["en-nbsp"]})).scalar()
    assert "'refus'" not in tsv, f"premise: this database parses an NBSP as a letter; got {tsv}"


async def _fts_matches(session, ids: dict[str, str]) -> tuple[set[str], set[str]]:
    """Labels whose fts_en matches EN_QUERY, and whose fts_fr matches FR_QUERY (no Python fold here)."""
    by_id = {v: k for k, v in ids.items()}
    found = []
    for column, config, query in (("fts_en", "english", EN_QUERY), ("fts_fr", "french", FR_QUERY)):
        rows = await session.execute(
            text(f"SELECT doc_id::text FROM chunks WHERE {column} @@ websearch_to_tsquery('{config}', :q)"),
            {"q": query},
        )
        found.append({by_id[r[0]] for r in rows})
    return found[0], found[1]


async def test_migration_recomputes_existing_rows_over_folded_text(locale_url):
    """At 0008 the NBSP- and U+202F-joined phrases are invisible to FTS (only the newline one
    matches); upgrading to 0009 recomputes the stored columns of the rows already there."""
    _alembic(locale_url, "downgrade", PRE_REVISION)
    try:
        async with _session(locale_url) as session:
            await _assert_nbsp_is_a_letter(session)
            ids = await _seed(session, TEXTS)
            await session.commit()
            assert await _fts_matches(session, ids) == (UNFOLDED_EN_LABELS, UNFOLDED_FR_LABELS)
            await session.commit()  # no open transaction: the upgrade takes ACCESS EXCLUSIVE on chunks

            _alembic(locale_url, "upgrade", "head")

            assert await _fts_matches(session, ids) == (EN_LABELS, FR_LABELS)
            await session.commit()
    finally:
        _alembic(locale_url, "upgrade", "head")


async def test_hybrid_search_finds_whitespace_joined_phrases_through_fts(locale_url, monkeypatch):
    """New rows at head, and hybrid_search with the embedder down: the FTS leg alone returns them."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(locale_url) as session:
        await _assert_nbsp_is_a_letter(session)
        ids = await _seed(session, TEXTS)
        assert await _fts_matches(session, ids) == (EN_LABELS, FR_LABELS)

        en = await search_mod.hybrid_search(session, EN_QUERY, k=20, bypass_reranker=True)
        assert {h.doc_id for h in en.hits} == {ids[label] for label in EN_LABELS}
        fr = await search_mod.hybrid_search(session, FR_QUERY, k=20, bypass_reranker=True)
        assert {h.doc_id for h in fr.hits} == {ids[label] for label in FR_LABELS}


@pytest.mark.parametrize("sep", [NBSP, NARROW_NBSP], ids=["nbsp", "narrow-nbsp"])
async def test_a_query_pasted_with_nbsps_is_folded(locale_url, monkeypatch, sep):
    """The query side: "first<NBSP>refusal" would parse to one lexeme and match nothing the folded
    columns hold. hybrid_search and kb_documents_by_date's FTS filter both fold it."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    async with _session(locale_url) as session:
        ids = await _seed(
            session, {"plain": "The distributor holds a right of first refusal.", "other": TEXTS["other"]}
        )
        query = f"first{sep}refusal"
        unfolded = (
            await session.execute(text("SELECT websearch_to_tsquery('english', :q)::text"), {"q": query})
        ).scalar()
        assert "&" not in unfolded, f"premise: the unfolded query is one lexeme; got {unfolded}"

        result = await search_mod.hybrid_search(session, query, k=10, bypass_reranker=True)
        assert [h.doc_id for h in result.hits] == [ids["plain"]]

        rows = await _query_documents_by_date(session, query=query, limit=10)
        assert [str(doc.doc_id) for doc, _, _ in rows] == [ids["plain"]]


# ---------------------------------------------------------------------------
# The live columns are the model's
# ---------------------------------------------------------------------------


def _model_expression(column: str) -> str:
    return str(Chunk.__table__.c[column].computed.sqltext)


def test_migration_sets_the_model_expressions_verbatim():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0009_fts_over_folded_text.py"
    spec = importlib.util.spec_from_file_location("_mig_0009", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from_migration = dict(module.FOLDED_EXPRESSIONS)
    assert from_migration == {c: _model_expression(c) for c in ("fts_en", "fts_fr")}


async def test_live_generation_expressions_match_the_model(db_session):
    """Compared as Postgres deparses them: a temp table built from the model's Computed text, against
    the migrated chunks table."""
    columns = ("fts_en", "fts_fr")
    generated = ", ".join(f"{c} tsvector GENERATED ALWAYS AS ({_model_expression(c)}) STORED" for c in columns)
    await db_session.execute(text(f"CREATE TEMP TABLE hc_model_chunks (chunk_text text NOT NULL, {generated})"))
    try:
        rows = await db_session.execute(
            text(
                "SELECT c.relname, a.attname, a.attgenerated::text, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid IN ('public.chunks'::regclass, 'hc_model_chunks'::regclass) "
                "AND a.attname IN ('fts_en', 'fts_fr')"
            )
        )
        found = {(r[0], r[1]): (r[2], r[3]) for r in rows}
    finally:
        await db_session.rollback()
    for column in columns:
        live = found[("chunks", column)]
        model = found[("hc_model_chunks", column)]
        assert live == model, column
        assert live[0] == "s" and "hc_fold_whitespace(COALESCE(chunk_text" in live[1], live
