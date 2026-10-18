"""#685: a search whose vector leg failed says so, on every surface.

The embedder failed for a whole benchmark night. Every search fell back to
keyword-only with a warning in a log nobody read; research then reported
"0 unique passages" for a topic the corpus covered, and three models told the
user their contracts did not discuss it. The fallback stays (one request should
not fail for it), but the result now carries `semantic_status`.
"""

import json
from contextlib import asynccontextmanager

import pytest
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


@pytest.fixture
async def unembedded_doc(db_session: AsyncSession, corpus):
    """A document keyword search finds whose nine passages have no vectors yet."""
    doc = Document(
        title="Keeper handbook",
        status="active",
        sha256=b"semantic-status-unembedded-00000",
        pipeline_status=PipelineStatus.embedding,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    for n in range(9):
        db_session.add(
            Chunk(doc_id=doc.doc_id, chunk_num=n, chunk_text=f"Lighthouse keeper duty {n}.", language="english")
        )
    await db_session.commit()
    return doc


async def test_low_vector_coverage_among_candidates_is_partial(db_session, unembedded_doc, monkeypatch):
    """The query embeds fine, but most of what keyword search found has no vectors:
    the semantic leg saw a tenth of it (#685's 29% unembedded corpus)."""
    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    result = await search_mod.hybrid_search(db_session, "lighthouse keeper")

    assert result.semantic_status == "partial"
    assert result.semantic_coverage == 0.1


async def test_rest_and_mcp_report_partial_coverage(
    client, admin_token, unembedded_doc, mcp_session, human, monkeypatch
):
    from harbor_clerk.mcp_server import kb_search

    monkeypatch.setattr(search_mod, "_embed_query", _embedder_up)

    rest = (
        await client.post("/api/search", json={"query": "lighthouse keeper"}, headers=auth_header(admin_token))
    ).json()
    mcp = json.loads(await kb_search(query="lighthouse keeper"))

    assert (rest["semantic_status"], rest["semantic_coverage"]) == ("partial", 0.1)
    assert (mcp["semantic_status"], mcp["semantic_coverage"]) == ("partial", 0.1)
    assert "not embedded" in mcp["semantic_notice"]


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
