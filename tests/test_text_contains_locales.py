"""text_contains end to end on databases with the locales that change its behaviour (#819).

LC_CTYPE C, like the Mac app's cluster (``initdb --locale=C``): ``to_tsvector`` reads a non-breaking
space as a letter, so "right of first refusal" joined by NBSPs is one lexeme and the FTS leg never
proposes the chunk for a query about "first refusal". The whitespace fold in ``text_contains``
reaches such a chunk only through the vector leg, which applies the same filter. Under C,
``[[:alnum:]]`` is also ASCII only, which the word-boundary recheck must not depend on.

UTF-8 (the builtin ``C.UTF-8`` locale, identical on macOS and Linux): ``lower()``, and so ILIKE,
folds case across Unicode (Kelvin sign, dotted capital I, capital sharp s, Ohm sign), which the
recheck must follow.

Each runs on its own private database (created from template0, migrated to head, dropped
afterwards) because the suite's database may be either (CI's pgvector image is en_US.utf8). The
vector-leg tests stub the query embedding the way the suite's other vector-leg tests do: the
embedder is not running under test.

What the fold does not fix: with the embedder down, or with the chunk outside the vector leg's
candidate pool, an NBSP-joined phrase on a C database is still unreachable, because the FTS side
indexes the unfolded text.
"""

import json
import os
import subprocess
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import harbor_clerk.search as search_mod
from harbor_clerk.api.deps import Principal
from harbor_clerk.models import Base, Chunk, Document
from harbor_clerk.models.enums import PipelineStatus

NBSP = chr(0xA0)
PHRASE_CHUNK = f"The distributor holds a right{NBSP}of{NBSP}first{NBSP}refusal over new territories."
OTHER_CHUNK = "The distributor holds an option over new territories."

C_LOCALE_SQL = "LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C'"
UTF8_LOCALE_SQL = "LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8' LC_COLLATE 'C' LC_CTYPE 'C'"


def _drop_database(admin, name: str) -> None:
    """Plain DROP first; FORCE (which terminates sessions) only if something still holds it open."""
    with admin.connect() as conn:
        try:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        except OperationalError:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _private_database(suffix: str, locale_sql: str):
    """Create, migrate, yield the URL of, and finally drop ``<test db>_<suffix>_test``."""
    base = make_url(os.environ["DATABASE_URL"])
    # Ends in _test, as conftest requires of any database the suite writes to.
    name = f"{base.database}_{suffix}_test"
    url = base.set(database=name)
    admin = create_engine(base.set(drivername="postgresql+psycopg2", database="postgres"), isolation_level="AUTOCOMMIT")
    ext_engine = None
    try:
        _drop_database(admin, name)
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8' {locale_sql}"))
        ext_engine = create_engine(url.set(drivername="postgresql+psycopg2"), isolation_level="AUTOCOMMIT")
        with ext_engine.connect() as conn:
            for ext in ("vector", "pg_trgm", "citext"):
                conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
        ext_engine.dispose()
        subprocess.run(
            ["alembic", "upgrade", "head"],
            check=True,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
        )
        yield url
    finally:
        if ext_engine is not None:
            ext_engine.dispose()
        _drop_database(admin, name)
        admin.dispose()


@pytest.fixture(scope="module")
def c_locale_url():
    yield from _private_database("ctype_c", C_LOCALE_SQL)


@pytest.fixture(scope="module")
def utf8_url():
    yield from _private_database("utf8", UTF8_LOCALE_SQL)


@asynccontextmanager
async def _session(url):
    engine = create_async_engine(url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
            yield session
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name != "schema_metadata":
                    await conn.execute(table.delete())
    finally:
        await engine.dispose()


@pytest.fixture
async def c_session(c_locale_url):
    async with _session(c_locale_url) as session:
        yield session


@pytest.fixture
async def utf8_session(utf8_url):
    async with _session(utf8_url) as session:
        yield session


@pytest.fixture(params=["c", "utf8"])
async def locale_session(request):
    url = request.getfixturevalue("c_locale_url" if request.param == "c" else "utf8_url")
    async with _session(url) as session:
        yield session


async def _seed(session, texts: dict[str, str], *, embedded: bool = False) -> dict[str, str]:
    ids = {}
    for label, chunk_text in texts.items():
        doc = Document(
            title=label,
            status="active",
            sha256=uuid.uuid4().bytes * 2,
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        session.add(doc)
        await session.flush()
        session.add(
            Chunk(
                doc_id=doc.doc_id,
                chunk_num=0,
                chunk_text=chunk_text,
                language="english",
                embedding=[0.01] * 768 if embedded else None,
            )
        )
        ids[label] = str(doc.doc_id)
    await session.flush()
    return ids


async def _embedder_down(query):
    raise ConnectionError("embedder not running")


async def _embed_stub(query):
    return [0.01] * 768


async def _assert_cases(session, ids, cases):
    for phrase, expected in cases.items():
        result = await search_mod.find_all(session, "covenant", max_results=20, text_contains=phrase)
        assert {h.doc_id for h in result.hits} == {ids[label] for label in expected}, repr(phrase)


# ---------------------------------------------------------------------------
# C: the NBSP phrase is reachable only through the vector leg
# ---------------------------------------------------------------------------


async def test_database_is_c_and_fts_cannot_see_the_phrase(c_session):
    """The premise: on LC_CTYPE C the NBSP-joined phrase is one lexeme, so FTS cannot match its words."""
    ctype = (
        await c_session.execute(text("SELECT datctype FROM pg_database WHERE datname = current_database()"))
    ).scalar()
    assert ctype == "C"
    tsv = (await c_session.execute(text("SELECT to_tsvector('english', :t)::text"), {"t": PHRASE_CHUNK})).scalar()
    assert "'refus'" not in tsv and "'first'" not in tsv, tsv


async def test_find_all_reaches_the_nbsp_phrase_through_the_vector_leg(c_session, monkeypatch):
    ids = await _seed(c_session, {"phrase": PHRASE_CHUNK, "other": OTHER_CHUNK}, embedded=True)

    # FTS alone (embedder down): the chunk is never a candidate, filter or not.
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    lexical = await search_mod.find_all(c_session, "first refusal", max_results=10, text_contains="first refusal")
    assert lexical.hits == []

    # With the vector leg: both chunks are candidates, and the folded filter keeps the phrase.
    monkeypatch.setattr(search_mod, "_embed_query", _embed_stub)
    unfiltered = await search_mod.find_all(c_session, "first refusal", max_results=10)
    assert {h.doc_id for h in unfiltered.hits} == set(ids.values())
    result = await search_mod.find_all(c_session, "first refusal", max_results=10, text_contains="first refusal")
    assert [h.doc_id for h in result.hits] == [ids["phrase"]]


async def test_kb_find_all_reaches_the_nbsp_phrase_through_the_vector_leg(c_session, monkeypatch):
    from harbor_clerk.mcp_server import _mcp_principal, kb_find_all

    ids = await _seed(c_session, {"phrase": PHRASE_CHUNK, "other": OTHER_CHUNK}, embedded=True)
    conn = await c_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)
    monkeypatch.setattr(search_mod, "_embed_query", _embed_stub)
    token = _mcp_principal.set(Principal(type="user", id=uuid.uuid4(), role="admin"))
    try:
        payload = json.loads(await kb_find_all(query="first refusal", max_results=10, text_contains="first refusal"))
    finally:
        _mcp_principal.reset(token)
    assert [r["doc_id"] for r in payload["results"]] == [ids["phrase"]]


# ---------------------------------------------------------------------------
# Word boundaries next to non-ASCII letters, in both locales
# ---------------------------------------------------------------------------

C_CEDILLA = chr(0xE7)
E_ACUTE = chr(0xE9)
COMBINING_ACUTE = chr(0x301)
LEFT_GUILLEMET = chr(0xAB)
RIGHT_GUILLEMET = chr(0xBB)


async def test_word_boundary_treats_accented_letters_and_marks_as_word(locale_session, monkeypatch):
    """Under C [[:alnum:]] is ASCII only, and in every locale a combining mark is not alnum: the
    boundary class lists Latin letters and combining marks so neither ends a word."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    ids = await _seed(
        locale_session,
        {
            "recu": f"The covenant was re{C_CEDILLA}u today.",
            "re-word": "The covenant le re dit today.",
            "etat": f"The covenant {E_ACUTE}tat today.",
            "cafe-nfd": f"The covenant cafe{COMBINING_ACUTE} today.",
            "cafe-nfc": f"The covenant caf{E_ACUTE} today.",
            "guillemets": f"The covenant {LEFT_GUILLEMET}IP{RIGHT_GUILLEMET} today.",
        },
    )
    await _assert_cases(
        locale_session,
        ids,
        {
            # Premises: the indexed substring match alone finds each of them.
            "re": {"recu", "re-word"},
            "tat": {"etat"},
            "cafe": {"cafe-nfd"},
            "caf": {"cafe-nfd", "cafe-nfc"},
            # The boundary recheck.
            " re ": {"re-word"},
            " tat ": set(),
            " cafe ": set(),
            " caf ": set(),
            " IP ": {"guillemets"},
        },
    )


# ---------------------------------------------------------------------------
# UTF-8: the recheck folds case the way ILIKE does
# ---------------------------------------------------------------------------

KELVIN_SIGN = chr(0x212A)
DOTTED_CAPITAL_I = chr(0x130)
CAPITAL_SHARP_S = chr(0x1E9E)
SHARP_S = chr(0xDF)
OHM_SIGN = chr(0x2126)
SMALL_OMEGA = chr(0x3C9)


async def test_word_boundary_recheck_folds_case_like_ilike(utf8_session, monkeypatch):
    """ILIKE compares lower() of both sides; ~* would only try the pattern character's own case
    variants and drop rows the ILIKE accepted."""
    lowered = (await utf8_session.execute(text("SELECT lower(:v)"), {"v": DOTTED_CAPITAL_I + KELVIN_SIGN})).scalar()
    assert lowered == "ik", "premise: this database folds case across Unicode"
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    ids = await _seed(
        utf8_session,
        {
            "kelvin": f"The covenant rates 300 {KELVIN_SIGN} here.",
            "istanbul": f"The covenant venue is {DOTTED_CAPITAL_I}stanbul today.",
            "strasse": f"The covenant names STRA{CAPITAL_SHARP_S}E street.",
            "ohm": f"The covenant measures 5 {OHM_SIGN} load.",
        },
    )
    await _assert_cases(
        utf8_session,
        ids,
        {
            # Premises: ILIKE alone matches across the case mapping.
            "300 k": {"kelvin"},
            "istanbul": {"istanbul"},
            f"stra{SHARP_S}e": {"strasse"},
            f"5 {SMALL_OMEGA}": {"ohm"},
            # The same with edge whitespace, through the recheck.
            " k ": {"kelvin"},
            " istanbul ": {"istanbul"},
            f" stra{SHARP_S}e ": {"strasse"},
            f" {SMALL_OMEGA} ": {"ohm"},
        },
    )
