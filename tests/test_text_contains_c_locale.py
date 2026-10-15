"""text_contains end to end on a database like the Mac app's: LC_CTYPE C (#819).

The Mac app's cluster is ``initdb --locale=C``. There ``to_tsvector`` reads a non-breaking space as a
letter, so "right of first refusal" joined by NBSPs is one lexeme and the FTS leg never proposes the
chunk for a query about "first refusal". The whitespace fold in ``text_contains`` reaches such a
chunk only through the vector leg, which applies the same filter. These tests run on a dedicated
C-ctype database (created from template0, migrated to head, dropped afterwards) because the suite's
database may not be C (CI's pgvector image is en_US.utf8), and they stub the query embedding the way
the suite's other vector-leg tests do: the embedder is not running under test.

What the fold does not fix here: with the embedder down, or with the chunk outside the vector leg's
candidate pool, such a chunk is still unreachable, because the FTS side indexes the unfolded text.
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
from harbor_clerk.search import find_all

NBSP = chr(0xA0)
PHRASE_CHUNK = f"The distributor holds a right{NBSP}of{NBSP}first{NBSP}refusal over new territories."
OTHER_CHUNK = "The distributor holds an option over new territories."


def _drop_database(admin, name: str) -> None:
    """Plain DROP first; FORCE (which terminates sessions) only if something still holds it open."""
    with admin.connect() as conn:
        try:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        except OperationalError:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def c_locale_url():
    base = make_url(os.environ["DATABASE_URL"])
    # Ends in _test, as conftest requires of any database the suite writes to.
    name = f"{base.database}_ctype_c_test"
    url = base.set(database=name)
    admin = create_engine(base.set(drivername="postgresql+psycopg2", database="postgres"), isolation_level="AUTOCOMMIT")
    ext_engine = None
    try:
        _drop_database(admin, name)
        with admin.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8' "
                    "LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C'"
                )
            )
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


@pytest.fixture
async def c_session(c_locale_url):
    engine = create_async_engine(c_locale_url.render_as_string(hide_password=False), poolclass=NullPool)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name != "schema_metadata":
                await conn.execute(table.delete())
    await engine.dispose()


async def _seed(session) -> dict[str, str]:
    ids = {}
    for label, chunk_text in (("phrase", PHRASE_CHUNK), ("other", OTHER_CHUNK)):
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
            Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=chunk_text, language="english", embedding=[0.01] * 768)
        )
        ids[label] = str(doc.doc_id)
    await session.flush()
    return ids


async def _embedder_down(query):
    raise ConnectionError("embedder not running")


async def _embed_stub(query):
    return [0.01] * 768


async def test_database_is_c_and_fts_cannot_see_the_phrase(c_session):
    """The premise: on LC_CTYPE C the NBSP-joined phrase is one lexeme, so FTS cannot match its words."""
    ctype = (
        await c_session.execute(text("SELECT datctype FROM pg_database WHERE datname = current_database()"))
    ).scalar()
    assert ctype == "C"
    tsv = (await c_session.execute(text("SELECT to_tsvector('english', :t)::text"), {"t": PHRASE_CHUNK})).scalar()
    assert "'refus'" not in tsv and "'first'" not in tsv, tsv


async def test_find_all_reaches_the_nbsp_phrase_through_the_vector_leg(c_session, monkeypatch):
    ids = await _seed(c_session)

    # FTS alone (embedder down): the chunk is never a candidate, filter or not.
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)
    lexical = await find_all(c_session, "first refusal", max_results=10, text_contains="first refusal")
    assert lexical.hits == []

    # With the vector leg: both chunks are candidates, and the folded filter keeps the phrase.
    monkeypatch.setattr(search_mod, "_embed_query", _embed_stub)
    unfiltered = await find_all(c_session, "first refusal", max_results=10)
    assert {h.doc_id for h in unfiltered.hits} == set(ids.values())
    result = await find_all(c_session, "first refusal", max_results=10, text_contains="first refusal")
    assert [h.doc_id for h in result.hits] == [ids["phrase"]]


async def test_kb_find_all_reaches_the_nbsp_phrase_through_the_vector_leg(c_session, monkeypatch):
    from harbor_clerk.mcp_server import _mcp_principal, kb_find_all

    ids = await _seed(c_session)
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
