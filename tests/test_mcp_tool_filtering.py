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
