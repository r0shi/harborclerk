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


async def _seed_long_docs(
    db_session, n: int, *, literal: str = "governed by the laws of Delaware", title_len: int | None = None
):
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    texts = {}
    for i in range(n):
        title = f"Long Agreement {i:03d}"
        if title_len is not None:  # CUAD-length titles: a title appears three times per row
            title = (title + " Distributor Agreement Exhibit 10.1 " * 5)[:title_len]
        doc = Document(
            title=title,
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
        assert row["source"]["chunk_id"]  # the snippet's chunk, to read before quoting

    full = json.loads(await kb_find_all(query="agreement", text_contains="governed by", presentation="full"))
    for row in full["results"]:
        assert "snippet" not in row
        assert row["top_chunk"]["text"] == texts[row["doc_id"]]


def _no_snippet_copy(raw: str) -> str:
    """The same kb_find_all result as it would be without snippets (rows as before #820)."""
    data = json.loads(raw)
    for row in data["results"]:
        row.pop("snippet", None)
        row["source"].pop("chunk_id", None)
    return json.dumps(data, indent=2)


@pytest.mark.parametrize("context_tokens", [16_384, 32_768, 65_536, 131_072])
async def test_chat_cut_drops_snippets_before_rows(db_session, admin_user, mock_session_factory, context_tokens):
    """Chat's per-result cut removes every find-all snippet (and its brief chunk id) before it
    drops a row: with CUAD-length titles, whenever the cut happens it keeps exactly as many rows
    as the same result without snippets, and no row keeps a snippet."""
    from harbor_clerk.llm.chat import _truncate_for_llm
    from harbor_clerk.llm.tools import execute_tool, summarize_tool_result

    await _seed_long_docs(db_session, 100, title_len=80)
    raw = await execute_tool(
        "find_all_documents", {"query": "agreement", "max_results": 100}, user_id=admin_user.user_id
    )
    payload = json.loads(raw)
    assert payload["returned"] == 100
    assert all(row["snippet"] and row["source"]["chunk_id"] for row in payload["results"])
    assert summarize_tool_result(raw) == "Found 100 results"

    # chat.py: tool_result_max_chars = min(int(context_tokens * 0.25 * 3.5), 80_000).
    cap = min(int(context_tokens * 0.25 * 3.5), 80_000)
    assert len(raw) > cap, "100 rows with snippets must exceed the cap for the test to mean anything"
    cut = json.loads(_truncate_for_llm(raw, cap))
    baseline = json.loads(_truncate_for_llm(_no_snippet_copy(raw), cap))
    assert len(json.dumps(cut, ensure_ascii=False)) <= cap
    assert len(cut["results"]) == len(baseline["results"]), (context_tokens, len(cut["results"]))
    assert cut["results"], "some rows must fit"
    for row in cut["results"]:
        assert "snippet" not in row and "chunk_id" not in row["source"]


async def test_chat_cut_keeps_every_snippet_when_the_result_fits(db_session, admin_user, mock_session_factory):
    """A find-all result within the cap is passed through whole, snippets and all."""
    from harbor_clerk.llm.chat import _truncate_for_llm
    from harbor_clerk.llm.tools import execute_tool

    await _seed_long_docs(db_session, 20, title_len=80)
    raw = await execute_tool(
        "find_all_documents", {"query": "agreement", "max_results": 100}, user_id=admin_user.user_id
    )
    cap = min(int(131_072 * 0.25 * 3.5), 80_000)
    assert len(raw) <= cap
    kept = json.loads(_truncate_for_llm(raw, cap))["results"]
    assert len(kept) == 20
    assert all(row["snippet"] and row["source"]["chunk_id"] for row in kept)


async def test_chat_cut_keeps_rows_when_only_the_snippets_overflow(db_session, admin_user, mock_session_factory):
    """Between the two sizes: the result fits without its snippets, so every row stays."""
    from harbor_clerk.llm.chat import _truncate_for_llm
    from harbor_clerk.llm.tools import execute_tool

    await _seed_long_docs(db_session, 40, title_len=80)
    raw = await execute_tool(
        "find_all_documents", {"query": "agreement", "max_results": 100}, user_id=admin_user.user_id
    )
    without = json.dumps(json.loads(_no_snippet_copy(raw)), ensure_ascii=False)
    cap = len(without) + 10
    assert len(raw) > cap
    cut = json.loads(_truncate_for_llm(raw, cap))
    assert len(cut["results"]) == 40
    assert "_truncated" not in cut
    assert all("snippet" not in row for row in cut["results"])


def _key_principal(cap: int | None) -> Principal:
    import uuid

    from harbor_clerk.api.scope import KeyScope

    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="search",
        tool_overrides={},
        max_snippet_chars=cap,
        rate_limit_rpm=None,
        rate_limit_rph=None,
    )
    return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)


@pytest.mark.parametrize("cap", [50, 1000, 0, -5])
async def test_kb_find_all_capped_key_gets_the_chunk_prefix(db_session, mock_session_factory, cap):
    """A key with max_snippet_chars gets the chunk's prefix within min(cap, 240), never a window
    on text_contains (which would let it slide through a chunk N characters at a time); a cap of
    0 or less omits the snippet and its chunk id."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.search import _FIND_ALL_SNIPPET_MAX_CHARS

    texts = await _seed_long_docs(db_session, 3)
    token = _mcp_principal.set(_key_principal(cap))
    try:
        payload = json.loads(await kb_find_all(query="agreement", text_contains="governed by", max_results=10))
    finally:
        _mcp_principal.reset(token)

    assert {r["doc_id"] for r in payload["results"]} == set(texts)
    for row in payload["results"]:
        if cap <= 0:
            assert "snippet" not in row and "chunk_id" not in row["source"]
            continue
        bound = min(cap, _FIND_ALL_SNIPPET_MAX_CHARS)
        text = texts[row["doc_id"]]
        assert text.index("governed by") > bound, "the literal must lie past the prefix"
        assert 0 < len(row["snippet"]) <= bound
        assert "governed by" not in row["snippet"]
        assert " ".join(text[:bound].split()).startswith(row["snippet"].removesuffix("\u2026"))


async def test_kb_find_all_source_chunk_id_without_source_context(
    db_session, mcp_principal, mock_session_factory, monkeypatch
):
    """A brief hit whose document has no SourceRef context still names the snippet's chunk (fallback path)."""
    import uuid
    from datetime import UTC, datetime

    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.search_types import FindAllHit, FindAllResult

    chunk_id = str(uuid.uuid4())

    async def fake_find_all(*args, **kwargs):
        hit = FindAllHit(
            doc_id=str(uuid.uuid4()),  # no such document: the source context has no entry for it
            doc_title="Orphan",
            mime_type="text/plain",
            language="en",
            score=0.5,
            ingested_at=datetime(2026, 6, 1, tzinfo=UTC),
            page_range="3",
            top_chunk_id=None,
            top_chunk_text=None,
            top_chunk_page=None,
            top_chunk_heading=None,
            snippet="an orphan passage",
            snippet_chunk_id=chunk_id,
        )
        return FindAllResult(
            hits=[hit], total_matches=1, offset=0, truncated=False, sort_by="relevance", presentation="brief"
        )

    monkeypatch.setattr("harbor_clerk.mcp_server.find_all", fake_find_all)
    (row,) = json.loads(await kb_find_all(query="orphan"))["results"]
    assert row["snippet"] == "an orphan passage"
    assert row["source"]["chunk_id"] == chunk_id


async def test_brief_row_source_chunk_id_reads_back_the_snippets_passage(
    db_session, mcp_principal, mock_session_factory
):
    """The docstring tells the model to read a brief row's passage with kb_read_passages before
    quoting it: source.chunk_id must name the chunk the snippet was cut from."""
    from harbor_clerk.mcp_server import kb_find_all, kb_read_passages

    texts = await _seed_long_docs(db_session, 2)
    payload = json.loads(await kb_find_all(query="agreement", text_contains="governed by", max_results=10))
    for row in payload["results"]:
        passages = json.loads(await kb_read_passages(chunk_ids=[row["source"]["chunk_id"]]))["passages"]
        (passage,) = passages
        assert passage["text"] == texts[row["doc_id"]]
        assert row["snippet"].strip("\u2026") in " ".join(passage["text"].split())


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
