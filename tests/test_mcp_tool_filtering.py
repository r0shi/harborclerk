"""Tests for MCP tool filtering by API key scope."""

import uuid

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import KeyScope


def make_scoped_key(tier="search", overrides=None):
    return Principal(
        type="api_key",
        id=uuid.uuid4(),
        role="user",
        key_scope=KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=None,
            permission_tier=tier,
            tool_overrides=overrides or {},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        ),
    )


def test_human_user_sees_all_tools():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    user = Principal(type="user", id=uuid.uuid4(), role="admin")
    all_tool_names = ["kb_search", "kb_read_passages", "kb_system_health", "kb_reprocess"]
    filtered = _filter_tools_for_principal(all_tool_names, user)
    assert set(filtered) == set(all_tool_names)


def test_search_tier_filters_to_search_tools():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("search")
    all_tool_names = ["kb_search", "kb_read_passages", "kb_system_health"]
    filtered = _filter_tools_for_principal(all_tool_names, p)
    assert "kb_search" in filtered
    assert "kb_read_passages" not in filtered
    assert "kb_system_health" not in filtered


def test_overrides_remove_tool():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("full", overrides={"kb_read_document": False})
    filtered = _filter_tools_for_principal(["kb_read_document", "kb_search"], p)
    assert "kb_read_document" not in filtered
    assert "kb_search" in filtered


def test_admin_only_tools_never_for_api_key_even_with_override():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("full", overrides={"kb_system_health": True, "kb_reprocess": True})
    filtered = _filter_tools_for_principal(["kb_system_health", "kb_reprocess", "kb_search"], p)
    assert "kb_system_health" not in filtered
    assert "kb_reprocess" not in filtered
    assert "kb_search" in filtered


def test_none_principal_returns_empty():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    assert _filter_tools_for_principal(["kb_search"], None) == []


# --- ScopedFastMCP dispatch layer ---

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_scoped_fastmcp_list_tools_filters_by_scope():
    """list_tools override returns only tools in the effective set."""
    from harbor_clerk.mcp_server import _mcp_principal, mcp

    p = make_scoped_key("search")
    token = _mcp_principal.set(p)
    try:
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        # search-tier tools present
        assert "kb_search" in tool_names
        assert "kb_batch_search" in tool_names
        # read-tier tools absent
        assert "kb_read_passages" not in tool_names
        # admin tools absent
        assert "kb_system_health" not in tool_names
    finally:
        _mcp_principal.reset(token)


@pytest.mark.asyncio
async def test_scoped_fastmcp_list_tools_no_principal_returns_empty():
    """No principal context → empty list (not full access)."""
    from harbor_clerk.mcp_server import mcp

    # Don't set _mcp_principal
    tools = await mcp.list_tools()
    # Should return empty — no principal means no access
    assert tools == []


@pytest.mark.asyncio
async def test_scoped_fastmcp_call_tool_rejects_disallowed():
    """call_tool raises ToolError when a scoped key invokes a disallowed tool."""
    from mcp.server.fastmcp.exceptions import ToolError

    from harbor_clerk.mcp_server import _mcp_principal, mcp

    p = make_scoped_key("search")  # no read tools allowed
    token = _mcp_principal.set(p)
    try:
        with pytest.raises(ToolError, match="Unknown tool"):
            await mcp.call_tool("kb_read_document", {"doc_id": "some-id"})
    finally:
        _mcp_principal.reset(token)


@pytest.mark.asyncio
async def test_scoped_fastmcp_human_user_sees_all():
    """Human users see all tools in list_tools, including admin tools."""
    from harbor_clerk.mcp_server import _mcp_principal, mcp

    user = Principal(type="user", id=uuid.uuid4(), role="admin")
    token = _mcp_principal.set(user)
    try:
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        # All 16 tools should be present for humans
        assert "kb_search" in tool_names
        assert "kb_read_passages" in tool_names
        assert "kb_system_health" in tool_names
        assert "kb_reprocess" in tool_names
    finally:
        _mcp_principal.reset(token)
