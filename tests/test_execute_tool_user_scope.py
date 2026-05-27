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
