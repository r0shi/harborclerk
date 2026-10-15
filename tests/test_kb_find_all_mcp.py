"""kb_find_all MCP tool — signature, response shape, server-side clamp."""

import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import _mcp_principal

# ---------------------------------------------------------------------------
# Fixtures (mirrored from test_mcp_tools.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_principal(admin_user):
    """Set _mcp_principal context var to an admin Principal for the test."""
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    yield
    _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to create proper AsyncSessions sharing the
    test connection, so lazy loading (greenlet-based) works correctly."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_kb_find_all_returns_dedupe_by_doc_id(db_session, mcp_principal, mock_session_factory):
    """End-to-end through the MCP tool path."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    # Seed 3 docs with 4 matching chunks each
    for i in range(3):
        doc = Document(
            title=f"D{i}",
            status="active",
            sha256=f"sha_mcp_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        db_session.add(doc)
        await db_session.flush()
        for j in range(4):
            db_session.add(
                Chunk(
                    doc_id=doc.doc_id,
                    chunk_num=j,
                    chunk_text="off-balance-sheet entity",
                    language="en",
                )
            )
    await db_session.flush()

    raw = await kb_find_all(query="off-balance-sheet", max_results=10)
    payload = json.loads(raw)

    assert len(payload["results"]) == 3
    assert payload["total_matches"] == 3
    assert payload["truncated"] is False
    assert payload["sort_by"] == "relevance"
    assert payload["presentation"] == "brief"
    seen = {r["doc_id"] for r in payload["results"]}
    assert len(seen) == 3


async def test_kb_find_all_clamps_at_settings_cap(db_session, mcp_principal, mock_session_factory, monkeypatch):
    """max_results above settings.find_all_max_results_cap is clamped.

    Seed 15 docs; cap to 10; request max_results=99999; expect exactly 10.
    """
    from harbor_clerk.config import get_settings
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    # Seed 15 distinct docs, each with one matching chunk
    for i in range(15):
        doc = Document(
            title=f"D{i}",
            status="active",
            sha256=f"sha_clamp_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="off-balance-sheet entity", language="en"))
    await db_session.flush()

    # Lower the cap to 10 for this test
    monkeypatch.setattr(get_settings(), "find_all_max_results_cap", 10)

    raw = await kb_find_all(query="off-balance-sheet", max_results=99999)
    payload = json.loads(raw)

    # 15 docs in corpus, cap clamps return to 10, total_matches reports full 15
    assert len(payload["results"]) == 10, f"clamp violated: got {len(payload['results'])}, expected exactly 10"
    assert payload["total_matches"] == 15
    assert payload["truncated"] is True


async def test_kb_find_all_response_shape(db_session, mcp_principal, mock_session_factory):
    """Response has all required top-level fields."""
    from harbor_clerk.mcp_server import kb_find_all

    raw = await kb_find_all(query="anything", max_results=10)
    payload = json.loads(raw)

    for key in ("results", "total_matches", "returned", "offset", "truncated", "sort_by", "presentation"):
        assert key in payload, f"Missing key: {key}"

    assert isinstance(payload["results"], list)
    assert isinstance(payload["total_matches"], int)
    assert isinstance(payload["returned"], int)
    assert isinstance(payload["offset"], int)
    assert isinstance(payload["truncated"], bool)


async def test_kb_find_all_per_hit_fields(db_session, mcp_principal, mock_session_factory):
    """Each hit in results has the expected per-document fields."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    doc = Document(
        title="BalanceSheetDoc",
        status="active",
        sha256=b"sha_hit_fields_test_12345_pad00",
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="balance sheet entry", language="en"))
    await db_session.flush()

    raw = await kb_find_all(query="balance sheet", max_results=10)
    payload = json.loads(raw)

    assert len(payload["results"]) >= 1
    hit = payload["results"][0]
    for key in ("doc_id", "doc_title", "mime_type", "language", "score", "ingested_at"):
        assert key in hit, f"Hit missing key: {key}"
    # score should be a number
    assert isinstance(hit["score"], (int, float))


async def test_kb_find_all_presentation_full_includes_top_chunk(db_session, mcp_principal, mock_session_factory):
    """presentation='full' adds top_chunk sub-object to each hit."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    doc = Document(
        title="FullPresDoc",
        status="active",
        sha256=b"sha_full_pres_test_12345_pad000",
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="quarterly earnings report", language="en"))
    await db_session.flush()

    raw = await kb_find_all(query="earnings", max_results=10, presentation="full")
    payload = json.loads(raw)

    assert payload["presentation"] == "full"
    if payload["results"]:
        hit = payload["results"][0]
        assert "top_chunk" in hit, "presentation=full must include top_chunk"
        tc = hit["top_chunk"]
        assert "chunk_id" in tc
        assert "text" in tc


async def test_kb_find_all_sort_by_date_desc(db_session, mcp_principal, mock_session_factory):
    """sort_by='date_desc' echoed back in response."""
    from harbor_clerk.mcp_server import kb_find_all

    raw = await kb_find_all(query="test", sort_by="date_desc")
    payload = json.loads(raw)
    assert payload["sort_by"] == "date_desc"


async def test_kb_find_all_offset_echoed(db_session, mcp_principal, mock_session_factory):
    """offset is echoed in the response."""
    from harbor_clerk.mcp_server import kb_find_all

    raw = await kb_find_all(query="test", offset=5)
    payload = json.loads(raw)
    assert payload["offset"] == 5


async def test_kb_find_all_invalid_sort_by_returns_error(db_session, mcp_principal, mock_session_factory):
    """Invalid sort_by returns an error message."""
    from harbor_clerk.mcp_server import kb_find_all

    raw = await kb_find_all(query="test", sort_by="bogus")
    payload = json.loads(raw)
    assert "error" in payload


async def test_kb_find_all_text_contains_filters_results(db_session, mcp_principal, mock_session_factory):
    """text_contains restricts results to docs whose chunks contain the literal."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    # Doc A contains "off-balance-sheet" literally
    doc_a = Document(
        title="A",
        status="active",
        sha256=b"sha_tc_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc_a)
    await db_session.flush()
    db_session.add(
        Chunk(
            doc_id=doc_a.doc_id,
            chunk_num=0,
            chunk_text="The off-balance-sheet entity disclosed.",
            language="en",
        )
    )

    # Doc B is semantically similar but lacks the literal phrase
    doc_b = Document(
        title="B",
        status="active",
        sha256=b"sha_tc_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc_b)
    await db_session.flush()
    db_session.add(
        Chunk(
            doc_id=doc_b.doc_id,
            chunk_num=0,
            chunk_text="The unconsolidated subsidiary disclosed.",
            language="en",
        )
    )
    await db_session.flush()

    # Without text_contains: both docs eligible
    raw = await kb_find_all(query="disclosed", max_results=10)
    payload = json.loads(raw)
    assert payload["total_matches"] == 2

    # With text_contains: only Doc A
    raw = await kb_find_all(
        query="disclosed",
        max_results=10,
        text_contains="off-balance-sheet",
    )
    payload = json.loads(raw)
    assert payload["total_matches"] == 1
    assert payload["results"][0]["doc_id"] == str(doc_a.doc_id)


async def test_kb_find_all_text_contains_ignores_whitespace(db_session, mcp_principal, mock_session_factory):
    """The MCP surface gets the whitespace fold too (#819): NBSPs and line breaks inside the phrase match."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    texts = {
        "nbsp": "The distributor holds a right\u00a0of\u00a0first\u00a0refusal.",
        "newline": "The distributor holds a first\nopportunity to bid.",
        "neither": "The distributor holds an option to bid.",
    }
    ids = {}
    for i, (label, chunk_text) in enumerate(texts.items()):
        doc = Document(
            title=label,
            status="active",
            sha256=f"sha_mcp_ws_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=chunk_text, language="english"))
        ids[label] = str(doc.doc_id)
    await db_session.flush()

    for phrase, label in (("first refusal", "nbsp"), ("first opportunity", "newline")):
        payload = json.loads(await kb_find_all(query="distributor", max_results=10, text_contains=phrase))
        assert [r["doc_id"] for r in payload["results"]] == [ids[label]], phrase


# ---------------------------------------------------------------------------
# presentation="brief" snippets (#820), on every surface that reads kb_find_all
# ---------------------------------------------------------------------------

_FILLER = "The supplier shall deliver the goods described in the schedule on the agreed dates. "


async def _seed_long_docs(db_session, n: int, *, literal: str = "governed by the laws of Delaware"):
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    texts = {}
    for i in range(n):
        doc = Document(
            title=f"Long Agreement {i:03d}",
            status="active",
            sha256=f"sha_snip_mcp_{i:019d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
        )
        db_session.add(doc)
        await db_session.flush()
        text = _FILLER * 6 + f"This Agreement {i} shall be {literal}. " + _FILLER * 6
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en"))
        texts[str(doc.doc_id)] = text
    await db_session.flush()
    return texts


async def test_kb_find_all_brief_payload_carries_bounded_snippet(db_session, mcp_principal, mock_session_factory):
    """Brief rows carry `snippet` (bounded, centred on text_contains) and no top_chunk; full rows the reverse."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.search import _FIND_ALL_SNIPPET_MAX_CHARS

    texts = await _seed_long_docs(db_session, 4)

    payload = json.loads(await kb_find_all(query="agreement", text_contains="Governed By The Laws", max_results=10))
    assert payload["presentation"] == "brief"
    assert {r["doc_id"] for r in payload["results"]} == set(texts)
    for row in payload["results"]:
        assert "top_chunk" not in row
        snippet = row["snippet"]
        assert len(snippet) <= _FIND_ALL_SNIPPET_MAX_CHARS
        assert "governed by the laws of Delaware" in snippet
        assert snippet.strip("…") in " ".join(texts[row["doc_id"]].split())
        assert row["citation"] and row["source"]["doc_id"] == row["doc_id"]  # the passage carries its citation

    full = json.loads(await kb_find_all(query="agreement", text_contains="governed by", presentation="full"))
    for row in full["results"]:
        assert "snippet" not in row
        assert row["top_chunk"]["text"] == texts[row["doc_id"]]


async def test_chat_find_all_result_carries_snippets_within_the_tool_result_budget(
    db_session, admin_user, mock_session_factory
):
    """The chat tool returns brief snippets; at a small model's per-result cap the result
    is cut by whole rows (valid JSON, every kept row still has its snippet), and the
    chip summary stays a one-line count."""
    from harbor_clerk.llm.chat import _truncate_for_llm
    from harbor_clerk.llm.tools import execute_tool, summarize_tool_result

    await _seed_long_docs(db_session, 100)

    raw = await execute_tool(
        "find_all_documents", {"query": "agreement", "max_results": 100}, user_id=admin_user.user_id
    )
    payload = json.loads(raw)
    assert payload["returned"] == 100
    assert all(row.get("snippet") for row in payload["results"])
    assert summarize_tool_result(raw) == "Found 100 results"

    # chat.py: tool_result_max_chars = min(int(context_tokens * 0.25 * 3.5), 80_000); 16K is the smallest slot.
    cap = min(int(16_384 * 0.25 * 3.5), 80_000)
    assert len(raw) > cap, "100 brief rows must exceed the cap for the test to mean anything"
    cut = _truncate_for_llm(raw, cap)
    assert len(cut) <= cap
    kept = json.loads(cut)
    assert kept["_truncated"] is True and kept["_original_count"] == 100
    assert kept["results"] and all(row.get("snippet") for row in kept["results"])


async def test_cli_renders_the_brief_snippet_of_a_real_payload(db_session, mcp_principal, mock_session_factory):
    """`harbor-clerk find-all` text output prints each brief snippet whole, one indented line per document."""
    import io

    from harbor_clerk.cli.output import OutputMode, render
    from harbor_clerk.mcp_server import kb_find_all

    await _seed_long_docs(db_session, 3)
    payload = json.loads(await kb_find_all(query="agreement", text_contains="governed by", max_results=10))

    buf = io.StringIO()
    render(payload, mode=OutputMode.TEXT, command="find-all", stream=buf)
    lines = buf.getvalue().splitlines()
    for row in payload["results"]:
        assert f"   {row['snippet']}" in lines, row["snippet"]

    buf = io.StringIO()
    render(payload, mode=OutputMode.JSON, command="find-all", stream=buf)
    assert [r["snippet"] for r in json.loads(buf.getvalue())["results"]] == [r["snippet"] for r in payload["results"]]
