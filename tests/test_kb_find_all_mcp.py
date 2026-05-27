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
    """max_results above settings.find_all_max_results_cap is clamped."""
    from harbor_clerk.config import get_settings
    from harbor_clerk.mcp_server import kb_find_all

    # Lower the cap to 10 for this test
    monkeypatch.setattr(get_settings(), "find_all_max_results_cap", 10)

    raw = await kb_find_all(query="x", max_results=99999)
    payload = json.loads(raw)
    # The returned set will be capped (regardless of corpus size).
    assert len(payload["results"]) <= 10


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
