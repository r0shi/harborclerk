"""#685: a search whose vector leg failed says so, on every surface.

The embedder failed for a whole benchmark night. Every search fell back to
keyword-only with a warning in a log nobody read; research then reported
"0 unique passages" for a topic the corpus covered, and three models told the
user their contracts did not discuss it. The fallback stays (one request should
not fail for it), but the result now carries `semantic_status`.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import harbor_clerk.search as search_mod
from harbor_clerk.api.deps import Principal
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from tests.conftest import auth_header

TEXT = "The lighthouse keeper must give ninety days of written notice before termination."


async def _embedder_down(_query):
    raise RuntimeError("embedder returned HTTP 500")


async def _embedder_up(_query):
    return [0.01] * 768


@pytest.fixture
async def corpus(db_session: AsyncSession):
    doc = Document(
        title="Keeper contract",
        status="active",
        sha256=b"semantic-status-fixture-00000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=TEXT, language="english", embedding=[0.01] * 768))
    await db_session.commit()
    return doc


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "false")


@pytest.fixture
async def mcp_session(db_session: AsyncSession, monkeypatch):
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.fixture
def human(admin_user):
    from harbor_clerk.mcp_server import _mcp_principal

    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    yield
    _mcp_principal.reset(token)


# --- the engine --------------------------------------------------------------


async def test_hybrid_search_flags_a_failed_vector_leg(db_session, corpus, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert result.hits, "the keyword fallback must still answer"
    assert result.semantic_status == "unavailable"


async def test_an_empty_degraded_result_is_flagged_too(db_session, corpus, monkeypatch):
    """ "Nothing found" is exactly the answer a missing vector leg falsifies."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    result = await search_mod.hybrid_search(db_session, "zeppelin")

    assert result.hits == []
    assert result.semantic_status == "unavailable"


async def test_a_healthy_search_reports_ok(db_session, corpus, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert result.hits
    assert result.semantic_status == "ok"
    assert result.semantic_coverage == 1.0


@pytest.fixture(autouse=True)
def _fresh_corpus_coverage():
    """The corpus coverage figure is cached process-wide; no test may read another's."""
    from harbor_clerk import embedding_coverage

    embedding_coverage.reset()
    yield
    embedding_coverage.reset()


async def _add_doc(db_session, title: str, texts: list[str], *, embedded: bool):
    doc = Document(
        title=title,
        status="active",
        sha256=title.encode().ljust(32, b"-")[:32],
        pipeline_status=PipelineStatus.ready if embedded else PipelineStatus.embedding,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    for n, text in enumerate(texts):
        db_session.add(
            Chunk(
                doc_id=doc.doc_id,
                chunk_num=n,
                chunk_text=text,
                language="english",
                embedding=[0.01] * 768 if embedded else None,
            )
        )
    await db_session.commit()
    return doc


@pytest.fixture
async def corpus_at_70_percent(db_session: AsyncSession, corpus):
    """10 chunks, 7 with vectors. The unembedded document shares no words with the
    test query, so every hit the query gets is an embedded chunk."""
    await _add_doc(db_session, "Quarterly revenue", [f"Revenue rose in quarter {n}." for n in range(6)], embedded=True)
    await _add_doc(db_session, "Tide tables", [f"High water at {n} o'clock." for n in range(3)], embedded=False)


async def test_a_partly_embedded_corpus_flags_partial_though_every_hit_is_embedded(
    db_session, corpus_at_70_percent, monkeypatch
):
    """The vector leg only ever returns embedded chunks, so coverage measured over
    what a query found reads 100% here; the corpus figure is what tells the reader
    that 30% of it is invisible to meaning-based search (#685: 29%)."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    hit_ids = [uuid.UUID(h.chunk_id) for h in result.hits]
    embedded = (
        await db_session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.chunk_id.in_(hit_ids), Chunk.embedding.isnot(None))
        )
    ).scalar_one()
    assert hit_ids and embedded == len(hit_ids), "premise: every hit is an embedded chunk"
    assert result.semantic_status == "partial"
    assert result.semantic_coverage == 0.7


async def test_a_95_percent_embedded_corpus_reads_ok(db_session, corpus, monkeypatch):
    await _add_doc(db_session, "Revenue", [f"Revenue rose in quarter {n}." for n in range(18)], embedded=True)
    await _add_doc(db_session, "Tides", ["High water at noon."], embedded=False)
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert result.semantic_status == "ok"
    assert result.semantic_coverage == 0.95


@pytest.fixture
async def half_embedded_corpus(db_session: AsyncSession, corpus):
    """2 chunks, 1 with a vector: the corpus reads 50%. The keeper contract is
    fully embedded; the tide log is not."""
    tide_log = await _add_doc(db_session, "Tide log", ["High water at noon."], embedded=False)
    return corpus, tide_log


async def test_no_document_filter_reads_the_corpus_figure(db_session, half_embedded_corpus, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert (result.semantic_status, result.semantic_coverage) == ("partial", 0.5)


async def test_a_date_filter_keeps_the_corpus_figure(db_session, half_embedded_corpus, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper", after=datetime(2000, 1, 1, tzinfo=UTC))

    assert (result.semantic_status, result.semantic_coverage) == ("partial", 0.5)


async def test_a_small_document_filter_reads_exact_coverage_over_its_documents(
    db_session, half_embedded_corpus, monkeypatch
):
    """A search limited to one fully embedded document must not warn because the
    rest of the corpus is half embedded; one limited to the unembedded one must."""
    keeper, tide_log = half_embedded_corpus
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    on_keeper = await search_mod.hybrid_search(db_session, "lighthouse keeper", doc_id=keeper.doc_id)
    on_log = await search_mod.hybrid_search(db_session, "high water", doc_ids=[tide_log.doc_id])

    assert (on_keeper.semantic_status, on_keeper.semantic_coverage) == ("ok", 1.0)
    assert (on_log.semantic_status, on_log.semantic_coverage) == ("partial", 0.0)


async def test_a_failed_filtered_coverage_query_costs_the_figure_not_the_search(
    db_session, half_embedded_corpus, monkeypatch, caplog
):
    """The search already has its results when coverage is computed. A database
    error there (here a real one, division by zero) reports no figure and is
    logged; the search's session stays usable for what follows."""
    import logging

    from sqlalchemy import text

    import harbor_clerk.embedding_coverage as coverage_mod

    keeper, _ = half_embedded_corpus
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    async def _failing(session, doc_ids):
        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(coverage_mod, "documents_embedding_coverage", _failing)

    with caplog.at_level(logging.ERROR, logger="harbor_clerk.embedding_coverage"):
        result = await search_mod.hybrid_search(db_session, "lighthouse keeper", doc_id=keeper.doc_id)

    assert result.hits, "a coverage failure failed the search"
    assert (result.semantic_status, result.semantic_coverage) == ("ok", None)
    assert any("coverage" in r.getMessage() and r.exc_info for r in caplog.records), caplog.text
    assert (await db_session.execute(select(func.count(Chunk.chunk_id)))).scalar_one() >= 0


async def test_a_large_document_filter_reports_no_coverage(db_session, half_embedded_corpus, monkeypatch):
    """Over EXACT_COVERAGE_MAX_DOCS the figure is not computed, and the corpus
    figure (which describes documents outside the filter) is not substituted."""
    from harbor_clerk.embedding_coverage import EXACT_COVERAGE_MAX_DOCS

    keeper, _ = half_embedded_corpus
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)
    many = [keeper.doc_id] + [uuid.uuid4() for _ in range(EXACT_COVERAGE_MAX_DOCS)]

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper", doc_ids=many)

    assert result.hits
    assert (result.semantic_status, result.semantic_coverage) == ("ok", None)


async def _scoped_key(db_session, docs) -> str:
    """A full-tier key scoped to one watched folder holding ``docs``; returns the raw key."""
    from harbor_clerk.auth import generate_api_key, hash_api_key
    from harbor_clerk.models import ApiKey
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

    folder = WatchedFolder(path=f"/scoped-{uuid.uuid4().hex}", display_name="Scoped", auto_discovered=False)
    db_session.add(folder)
    await db_session.flush()
    for n, doc in enumerate(docs):
        db_session.add(
            WatchedFile(
                folder_id=folder.folder_id,
                doc_id=doc.doc_id,
                relative_path=f"doc-{n}.txt",
                sha256=bytes([n % 256]) * 32,
                bookmark_data=b"",
                status=WatchedFileStatus.active,
            )
        )
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            name="scoped-coverage",
            key_hash=hash_api_key(raw),
            permission_tier="full",
            scope_folder_ids=[str(folder.folder_id)],
        )
    )
    await db_session.commit()
    return raw


async def _kb_search_as(raw_key: str, query: str) -> dict:
    from harbor_clerk.mcp_server import _mcp_principal, _resolve_principal, kb_search

    principal = await _resolve_principal(raw_key)
    token = _mcp_principal.set(principal)
    try:
        return json.loads(await kb_search(query=query))
    finally:
        _mcp_principal.reset(token)


async def test_a_scoped_key_never_sees_the_corpus_figure(db_session, half_embedded_corpus, monkeypatch):
    """The corpus is mostly unembedded. A key scoped to the fully embedded contract
    gets the exact figure for its scope (no warning); a key whose scope is over
    the exact-figure bound gets no figure at all, never the corpus's."""
    from harbor_clerk.embedding_coverage import EXACT_COVERAGE_MAX_DOCS, corpus_embedding_coverage

    keeper, _ = half_embedded_corpus
    filler = [
        await _add_doc(db_session, f"Filler {n}", [f"Lighthouse keeper filler {n}."], embedded=False)
        for n in range(EXACT_COVERAGE_MAX_DOCS)
    ]
    small_key = await _scoped_key(db_session, [keeper])
    large_key = await _scoped_key(db_session, [keeper, *filler])
    assert await corpus_embedding_coverage(db_session) < 0.1, "premise: the corpus figure would warn"
    # Bind the MCP tools to the test connection only now: the commits above end
    # the session's transaction and with it the connection a factory would hold.
    await _bind_mcp_to(db_session, monkeypatch)
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    small = await _kb_search_as(small_key, "lighthouse keeper")
    assert small["hits"], small
    assert "semantic_status" not in small and "semantic_coverage" not in small, small

    large = await _kb_search_as(large_key, "lighthouse keeper")
    assert large["hits"], large
    assert "semantic_coverage" not in large and large.get("semantic_status") != "partial", large


async def _bind_mcp_to(db_session, monkeypatch):
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


async def test_an_unavailable_vector_leg_stays_unavailable_not_partial(db_session, corpus_at_70_percent, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert result.semantic_status == "unavailable"
    assert result.semantic_coverage is None


async def test_concurrent_stale_reads_run_one_corpus_aggregate(db_session, monkeypatch):
    """The cold aggregate measured 4.1 s on 300k chunks: searches arriving while it
    runs must use the previous figure, not start more of them."""
    import asyncio

    from harbor_clerk import embedding_coverage

    aggregates: list[int] = []

    async def _slow_counts(_session):
        aggregates.append(1)
        await asyncio.sleep(0.2)
        return {"chunks_total": 10, "chunks_embedded": 7}

    monkeypatch.setattr(embedding_coverage, "embedding_coverage_counts", _slow_counts)

    shares = await asyncio.wait_for(
        asyncio.gather(*(embedding_coverage.corpus_embedding_coverage(db_session) for _ in range(5))), timeout=10
    )

    assert len(aggregates) == 1, f"{len(aggregates)} concurrent aggregates"
    assert 0.7 in shares and set(shares) <= {0.7, None}


async def test_the_corpus_figure_is_cached_not_computed_per_search(db_session, corpus_at_70_percent, monkeypatch):
    from harbor_clerk import embedding_coverage

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)
    real_counts = embedding_coverage.embedding_coverage_counts
    aggregates: list[int] = []

    async def _counting(session):
        aggregates.append(1)
        return await real_counts(session)

    clock = [5000.0]
    monkeypatch.setattr(embedding_coverage, "embedding_coverage_counts", _counting)
    monkeypatch.setattr(embedding_coverage, "_now", lambda: clock[0])

    for query in ("lighthouse keeper", "revenue", "zeppelin"):
        assert (await search_mod.hybrid_search(db_session, query)).semantic_status == "partial"
    assert len(aggregates) == 1, f"{len(aggregates)} aggregates for 3 searches inside the TTL"

    clock[0] += embedding_coverage.CORPUS_COVERAGE_TTL_SECONDS + 1
    stale = await search_mod.hybrid_search(db_session, "lighthouse keeper")
    assert stale.semantic_coverage == 0.7, "the stale figure is served while it is replaced"
    await asyncio.wait_for(embedding_coverage._refresh_task(), timeout=10)
    assert len(aggregates) == 2, "a stale figure was not refreshed"


def _cache_figure(embedding_coverage, share: float, *, age: float):
    """Seed the cache with ``share`` as computed ``age`` seconds ago."""
    embedding_coverage._cache = (embedding_coverage._now() - age, share)


async def test_a_stale_figure_is_returned_at_once_while_the_refresh_runs(db_session, monkeypatch):
    """The search that finds the figure stale must not pay the aggregate (4.1 s cold)."""
    import time

    from harbor_clerk import embedding_coverage

    async def _slow_counts(_session):
        await asyncio.sleep(1.0)
        return {"chunks_total": 10, "chunks_embedded": 9}

    monkeypatch.setattr(embedding_coverage, "embedding_coverage_counts", _slow_counts)
    _cache_figure(embedding_coverage, 0.4, age=embedding_coverage.CORPUS_COVERAGE_TTL_SECONDS + 5)

    started = time.monotonic()
    share = await embedding_coverage.corpus_embedding_coverage(db_session)
    elapsed = time.monotonic() - started

    assert share == 0.4
    assert elapsed < 0.5, f"the stale read waited {elapsed:.2f}s on the aggregate"
    task = embedding_coverage._refresh_task()
    assert task is not None and not task.done(), "no background refresh was started"
    await asyncio.wait_for(task, timeout=10)
    assert await embedding_coverage.corpus_embedding_coverage(db_session) == 0.9


async def test_the_first_search_waits_only_up_to_the_bound(db_session, monkeypatch):
    import time

    from harbor_clerk import embedding_coverage

    async def _slow_counts(_session):
        await asyncio.sleep(1.5)
        return {"chunks_total": 4, "chunks_embedded": 3}

    monkeypatch.setattr(embedding_coverage, "embedding_coverage_counts", _slow_counts)
    monkeypatch.setattr(embedding_coverage, "FIRST_FIGURE_WAIT_SECONDS", 0.2)

    started = time.monotonic()
    share = await embedding_coverage.corpus_embedding_coverage(db_session)
    elapsed = time.monotonic() - started

    assert share is None, "past the bound the first search reports no figure"
    assert elapsed < 1.0, f"the first search waited {elapsed:.2f}s, past its bound"
    task = embedding_coverage._refresh_task()
    assert task is not None and not task.done(), "the refresh must carry on in the background"
    await asyncio.wait_for(task, timeout=10)
    assert await embedding_coverage.corpus_embedding_coverage(db_session) == 0.75


async def test_a_failed_refresh_keeps_the_old_figure_and_is_logged(db_session, monkeypatch, caplog):
    import logging

    from harbor_clerk import embedding_coverage

    attempts: list[int] = []

    async def _failing_counts(_session):
        attempts.append(1)
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(embedding_coverage, "embedding_coverage_counts", _failing_counts)
    _cache_figure(embedding_coverage, 0.4, age=embedding_coverage.CORPUS_COVERAGE_TTL_SECONDS + 5)

    with caplog.at_level(logging.ERROR, logger="harbor_clerk.embedding_coverage"):
        assert await embedding_coverage.corpus_embedding_coverage(db_session) == 0.4
        task = embedding_coverage._refresh_task()
        await asyncio.wait_for(task, timeout=10)

    assert task.exception() is None, "the background task leaked its exception"
    assert any("refresh failed" in r.getMessage() and r.exc_info for r in caplog.records), caplog.records
    assert await embedding_coverage.corpus_embedding_coverage(db_session) == 0.4, "the old figure was lost"
    # Let any refresh that search started run before counting attempts.
    await asyncio.wait_for(embedding_coverage._refresh_task(), timeout=10)
    assert len(attempts) == 1, "a refresh that just failed was retried on the next search"


async def test_rest_and_mcp_report_partial_coverage(
    client, admin_token, corpus_at_70_percent, mcp_session, human, monkeypatch
):
    from harbor_clerk.mcp_server import kb_search

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    rest = (
        await client.post("/api/search", json={"query": "lighthouse keeper"}, headers=auth_header(admin_token))
    ).json()
    mcp = json.loads(await kb_search(query="lighthouse keeper"))

    assert (rest["semantic_status"], rest["semantic_coverage"]) == ("partial", 0.7)
    assert (mcp["semantic_status"], mcp["semantic_coverage"]) == ("partial", 0.7)
    assert "aren't searchable by meaning yet" in mcp["semantic_notice"]
    assert "the documents this search covers" in mcp["semantic_notice"]


async def test_find_all_carries_the_flag(db_session, corpus, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    result = await search_mod.find_all(db_session, "lighthouse")

    assert result.semantic_status == "unavailable"


# --- REST --------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/search", {"query": "lighthouse keeper"}),
        ("/api/search", {"query": "lighthouse keeper", "faceted": True}),
        ("/api/search", {"query": "zeppelin"}),
        ("/api/search/find-all", {"query": "lighthouse"}),
    ],
    ids=["search", "faceted", "empty", "find-all"],
)
async def test_rest_search_reports_semantic_unavailable(client, admin_token, corpus, monkeypatch, path, body):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    resp = await client.post(path, json=body, headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["semantic_status"] == "unavailable"


async def test_rest_search_reports_ok_when_healthy(client, admin_token, corpus, monkeypatch):
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    resp = await client.post("/api/search", json={"query": "lighthouse keeper"}, headers=auth_header(admin_token))

    assert resp.json()["semantic_status"] == "ok"


# --- MCP (and so chat and research, which call these in-process) ------------


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("kb_search", {"query": "lighthouse keeper"}),
        ("kb_search", {"query": "lighthouse keeper", "faceted": True}),
        ("kb_search", {"query": "zeppelin"}),
        ("kb_find_all", {"query": "lighthouse"}),
    ],
    ids=["kb_search", "faceted", "empty", "kb_find_all"],
)
async def test_mcp_search_reports_semantic_unavailable(corpus, mcp_session, human, monkeypatch, tool, kwargs):
    import harbor_clerk.mcp_server as m

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    payload = json.loads(await getattr(m, tool)(**kwargs))

    assert payload["semantic_status"] == "unavailable"
    assert "keyword" in payload["semantic_notice"], "the model must be told what the flag means"
    assert "error" not in payload, "a degraded result is still a result, not a failed call"


@pytest.mark.parametrize(
    "tool,kwargs,body_key",
    [
        ("kb_search", {"query": "lighthouse keeper"}, "hits"),
        ("kb_search", {"query": "lighthouse keeper", "faceted": True}, "documents"),
        ("kb_find_all", {"query": "lighthouse"}, "results"),
    ],
    ids=["kb_search", "faceted", "kb_find_all"],
)
async def test_the_semantic_notice_comes_before_the_results(
    corpus, mcp_session, human, monkeypatch, tool, kwargs, body_key
):
    """Chat's plain truncation cuts from the end: the notice must survive it."""
    import harbor_clerk.mcp_server as m

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    raw = await getattr(m, tool)(**kwargs)

    keys = list(json.loads(raw))
    assert keys.index("semantic_notice") < keys.index(body_key), keys
    assert raw.index('"semantic_notice"') < raw.index(f'"{body_key}"')


async def test_mcp_batch_search_flags_each_query(corpus, mcp_session, human, monkeypatch):
    from harbor_clerk.mcp_server import kb_batch_search

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_down)

    payload = json.loads(await kb_batch_search(queries=["lighthouse keeper", "zeppelin"]))

    assert [r["semantic_status"] for r in payload["results"]] == ["unavailable", "unavailable"]


async def test_a_healthy_mcp_payload_is_unchanged(corpus, mcp_session, human, monkeypatch):
    from harbor_clerk.mcp_server import kb_search

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    payload = json.loads(await kb_search(query="lighthouse keeper"))

    assert payload["hits"]
    assert "semantic_status" not in payload and "semantic_notice" not in payload


# --- chat chip and research notes ---------------------------------------------


def test_chat_tool_chip_says_keyword_only():
    from harbor_clerk.llm.tools import summarize_tool_result

    degraded = json.dumps({"hits": [{}], "semantic_status": "unavailable"})
    batch = json.dumps({"results": [{"hits": [], "semantic_status": "unavailable"}]})
    healthy = json.dumps({"hits": [{}]})

    assert summarize_tool_result(degraded) == "Found 1 results (keyword-only: semantic search unavailable)"
    assert summarize_tool_result(batch).endswith("(keyword-only: semantic search unavailable)")
    assert summarize_tool_result(healthy) == "Found 1 results"


def test_chat_tool_chip_flags_partial_and_puts_unavailable_first():
    from harbor_clerk.llm.tools import summarize_tool_result

    partial = json.dumps({"hits": [{}], "semantic_status": "partial", "semantic_coverage": 0.7})
    mixed = json.dumps(
        {"results": [{"hits": [], "semantic_status": "partial"}, {"hits": [], "semantic_status": "unavailable"}]}
    )

    assert summarize_tool_result(partial) == "Found 1 results (some documents not searchable by meaning)"
    assert summarize_tool_result(mixed).endswith("(keyword-only: semantic search unavailable)")


async def test_research_collects_semantic_status_from_pagination_pages(monkeypatch):
    """A query whose first page was healthy can page into a degraded leg (the
    embedder failed in between); the page's status counts too."""
    from harbor_clerk.llm import research as research_mod

    async def _execute_tool(name, arguments, user_id, **kwargs):
        if name == "batch_search":
            return json.dumps(
                {
                    "results": [
                        {"query": q, "hits": [{}] * 5, "has_more": True, "total_candidates": 50}
                        for q in arguments["queries"]
                    ]
                }
            )
        return json.dumps({"hits": [], "has_more": False, "semantic_status": "partial"})

    monkeypatch.setattr(research_mod, "execute_tool", _execute_tool)
    monkeypatch.setattr(research_mod, "_ingest_hits", lambda coverage, hits, query: None)
    keyword_only: list[str] = []
    partial: list[str] = []

    [
        s
        async for s in research_mod._search_fan_out_steps(
            ["deep"], None, 5, paginate=True, keyword_only_queries=keyword_only, partial_coverage_queries=partial
        )
    ]

    assert partial == ["deep"], "a pagination page's semantic_status was ignored"
    assert keyword_only == []


async def test_research_fan_out_collects_keyword_only_queries(monkeypatch):
    from harbor_clerk.llm import research as research_mod

    async def _execute_tool(name, arguments, user_id, **kwargs):
        return json.dumps(
            {
                "results": [
                    {"query": q, "hits": [], "semantic_status": "unavailable"}
                    if q == "down"
                    else {"query": q, "hits": []}
                    for q in arguments["queries"]
                ]
            }
        )

    monkeypatch.setattr(research_mod, "execute_tool", _execute_tool)
    keyword_only: list[str] = []

    steps = [
        s async for s in research_mod._search_fan_out_steps(["up", "down"], None, 5, keyword_only_queries=keyword_only)
    ]

    assert steps[-1][0] == "coverage"
    assert keyword_only == ["down"]
    note = research_mod._keyword_only_note(keyword_only, 2)
    assert "1 of 2 searches" in note and "not evidence" in note
    assert research_mod._keyword_only_note([], 2) == ""


@pytest.mark.parametrize(
    "command,payload",
    [
        ("search", {"hits": [], "semantic_status": "unavailable"}),
        ("find-all", {"results": [], "total_matches": 0, "semantic_status": "unavailable"}),
        ("batch-search", {"results": [{"query": "q", "hits": [], "semantic_status": "unavailable"}]}),
    ],
)
def test_cli_text_output_says_keyword_only(command, payload):
    import io

    from harbor_clerk.cli.output import OutputMode, render

    out = io.StringIO()
    render(payload, mode=OutputMode.TEXT, command=command, stream=out)

    assert "semantic_status=unavailable" in out.getvalue()


@pytest.mark.parametrize(
    "command,payload",
    [
        ("search", {"hits": [], "semantic_status": "partial", "semantic_coverage": 0.4}),
        ("find-all", {"results": [], "total_matches": 0, "semantic_status": "partial", "semantic_coverage": 0.4}),
        (
            "batch-search",
            {"results": [{"query": "q", "hits": [], "semantic_status": "partial", "semantic_coverage": 0.4}]},
        ),
    ],
)
def test_cli_text_output_says_partial(command, payload):
    import io

    from harbor_clerk.cli.output import OutputMode, render

    out = io.StringIO()
    render(payload, mode=OutputMode.TEXT, command=command, stream=out)

    assert "semantic_status=partial - some documents aren't searchable by meaning yet (40% of" in out.getvalue()
    # True with or without a document filter: never "the corpus" or "the library".
    assert "the documents this search covers" in out.getvalue()


async def test_research_records_partial_coverage_with_its_own_note(monkeypatch):
    """Partial is not keyword-only: the synthesis is told some documents aren't
    searchable by meaning yet, which is a different gap from an outage."""
    from harbor_clerk.llm import research as research_mod

    statuses = {"thin": "partial", "down": "unavailable"}

    async def _execute_tool(name, arguments, user_id, **kwargs):
        return json.dumps(
            {
                "results": [
                    {"query": q, "hits": [], **({"semantic_status": statuses[q]} if q in statuses else {})}
                    for q in arguments["queries"]
                ]
            }
        )

    monkeypatch.setattr(research_mod, "execute_tool", _execute_tool)
    keyword_only: list[str] = []
    partial: list[str] = []

    steps = [
        s
        async for s in research_mod._search_fan_out_steps(
            ["up", "thin", "down"], None, 5, keyword_only_queries=keyword_only, partial_coverage_queries=partial
        )
    ]

    assert steps[-1][0] == "coverage"
    assert (keyword_only, partial) == (["down"], ["thin"])
    note = research_mod._partial_coverage_note(partial, 3)
    assert "1 of 3 searches" in note and "aren't searchable by meaning yet" in note
    assert research_mod._partial_coverage_note([], 3) == ""
