"""Tests for scoped API keys."""

from harbor_clerk.api.scope import (
    ADMIN_ONLY_TOOLS,
    FULL_TIER_TOOLS,
    READ_TIER_TOOLS,
    SEARCH_TIER_TOOLS,
    KeyScope,
    compute_effective_tools,
)


def test_search_tier_has_search_tools():
    tools = compute_effective_tools("search", {})
    assert "kb_search" in tools
    assert "kb_batch_search" in tools
    assert "kb_corpus_overview" in tools
    assert "kb_list_recent" in tools
    # No read tools
    assert "kb_read_passages" not in tools
    # No admin tools
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_read_tier_extends_search():
    tools = compute_effective_tools("read", {})
    assert "kb_search" in tools  # search tier inherited
    assert "kb_read_passages" in tools
    assert "kb_expand_context" in tools
    assert "kb_document_outline" in tools
    assert "kb_get_document" in tools
    # Not in this tier
    assert "kb_find_related" not in tools
    assert "kb_entity_search" not in tools


def test_full_tier_has_most_tools():
    tools = compute_effective_tools("full", {})
    assert "kb_read_document" in tools
    assert "kb_entity_search" in tools
    assert "kb_find_related" in tools
    assert "kb_ingest_status" in tools
    # Admin tools never included
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_overrides_can_remove_tool():
    tools = compute_effective_tools("full", {"kb_read_document": False})
    assert "kb_read_document" not in tools
    assert "kb_search" in tools  # other tools unchanged


def test_overrides_can_add_tool():
    tools = compute_effective_tools("search", {"kb_read_passages": True})
    assert "kb_read_passages" in tools


def test_overrides_cannot_grant_admin_tools():
    tools = compute_effective_tools("full", {"kb_system_health": True})
    assert "kb_system_health" not in tools
    tools = compute_effective_tools("full", {"kb_reprocess": True})
    assert "kb_reprocess" not in tools


def test_unknown_tier_treated_as_full():
    # Defensive — unknown tier shouldn't crash
    tools = compute_effective_tools("nonsense", {})
    assert "kb_search" in tools


def test_keyscope_default_no_restrictions():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is True


def test_keyscope_with_topic_filter_is_restricted():
    scope = KeyScope(
        scope_topic_ids=[1, 2],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is False


def test_admin_only_tools_constant():
    assert "kb_system_health" in ADMIN_ONLY_TOOLS
    assert "kb_reprocess" in ADMIN_ONLY_TOOLS


def test_tier_constants_are_frozen_sets():
    assert isinstance(SEARCH_TIER_TOOLS, frozenset)
    assert isinstance(READ_TIER_TOOLS, frozenset)
    assert isinstance(FULL_TIER_TOOLS, frozenset)
