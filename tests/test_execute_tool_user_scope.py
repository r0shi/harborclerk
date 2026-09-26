"""Tests for execute_tool forwarding user_scope onto Principal."""

import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.scope import UserScope
from harbor_clerk.llm.tools import execute_tool


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to use the test connection so MCP tools
    see flushed/committed data from the test's db_session."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.mark.asyncio
async def test_execute_tool_corpus_overview_with_no_scope_sees_all(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """No user_scope = no filter = all docs from both folders visible in overview."""
    result_str = await execute_tool(
        "corpus_overview",
        {},
        user_id=admin_user.user_id,
    )
    result = json.loads(result_str)
    # Should see all 4 docs from two_folder_corpus
    assert result.get("document_count", 0) >= 4


@pytest.mark.asyncio
async def test_execute_tool_corpus_overview_with_folder_scope_restricts(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """user_scope=folder_a → only folder_a's 2 docs visible in overview."""
    folder_a, _, docs_in_a, _ = two_folder_corpus
    scope = UserScope(folder_ids=[folder_a.folder_id])

    result_str = await execute_tool(
        "corpus_overview",
        {},
        user_id=admin_user.user_id,
        user_scope=scope,
    )
    result = json.loads(result_str)
    # With scope to folder_a only, document_count should be exactly 2
    assert result.get("document_count") == 2


@pytest.mark.asyncio
async def test_execute_tool_documents_by_date_scope_restricts_before_the_sort_and_the_limit(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """#722's review: the date tool ignored the scope every other kb_* tool applies, so a folder-scoped chat saw
    the other folder's documents. And the scope must restrict the candidates before the sort and the limit:
    folder A holds the two oldest documents, B the two newest; "latest, two of them" scoped to A is A's two, which
    a filter applied to the ten returned rows afterwards would never find."""
    from datetime import UTC, datetime, timedelta

    folder_a, _, docs_in_a, docs_in_b = two_folder_corpus
    base = datetime(2001, 1, 1, tzinfo=UTC)
    for i, d in enumerate([*docs_in_a, *docs_in_b]):
        d.created_at = base + timedelta(days=30 * i)
    await db_session.flush()
    scope = UserScope(folder_ids=[folder_a.folder_id])

    scoped = json.loads(
        await execute_tool(
            "documents_by_date", {"direction": "latest", "limit": 2}, user_id=admin_user.user_id, user_scope=scope
        )
    )
    assert [r["doc_id"] for r in scoped["results"]] == [str(d.doc_id) for d in reversed(docs_in_a)], scoped

    unscoped = json.loads(
        await execute_tool("documents_by_date", {"direction": "latest", "limit": 2}, user_id=admin_user.user_id)
    )
    assert [r["doc_id"] for r in unscoped["results"]] == [str(d.doc_id) for d in reversed(docs_in_b)], unscoped


@pytest.mark.asyncio
async def test_kb_documents_by_date_honours_a_scoped_api_key_and_an_empty_scope(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """The MCP surface: a key scoped to folder A sees A; a key scoped to a folder with no documents sees none,
    and an invalid direction is still an error there, not a silent empty page."""
    import uuid

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.api.scope import KeyScope
    from harbor_clerk.mcp_server import _mcp_principal, kb_documents_by_date
    from harbor_clerk.models.watched import WatchedFolder

    folder_a, _, docs_in_a, _ = two_folder_corpus
    empty = WatchedFolder(path="/c", display_name="Folder C", auto_discovered=False)
    db_session.add(empty)
    await db_session.flush()

    def key_for(folder_id) -> Principal:
        scope = KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=[str(folder_id)],
            permission_tier="search",
            tool_overrides={},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        )
        return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)

    token = _mcp_principal.set(key_for(folder_a.folder_id))
    try:
        seen = json.loads(await kb_documents_by_date(direction="earliest", limit=50))
    finally:
        _mcp_principal.reset(token)
    assert {r["doc_id"] for r in seen["results"]} == {str(d.doc_id) for d in docs_in_a}

    token = _mcp_principal.set(key_for(empty.folder_id))
    try:
        none = json.loads(await kb_documents_by_date(direction="earliest", limit=50))
        bad = json.loads(await kb_documents_by_date(direction="sideways", limit=50))
    finally:
        _mcp_principal.reset(token)
    assert none["count"] == 0 and none["results"] == []
    assert "error" in bad and "direction" in bad["error"]
