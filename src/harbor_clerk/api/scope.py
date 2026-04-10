"""Per-API-key scope computation: tiers, tool sets, and query filters."""

from dataclasses import dataclass

# Tier -> base tool set
SEARCH_TIER_TOOLS: frozenset[str] = frozenset({"kb_search", "kb_batch_search", "kb_corpus_overview", "kb_list_recent"})
READ_TIER_TOOLS: frozenset[str] = SEARCH_TIER_TOOLS | frozenset(
    {"kb_read_passages", "kb_expand_context", "kb_document_outline", "kb_get_document"}
)
FULL_TIER_TOOLS: frozenset[str] = READ_TIER_TOOLS | frozenset(
    {
        "kb_read_document",
        "kb_find_related",
        "kb_entity_search",
        "kb_entity_overview",
        "kb_entity_cooccurrence",
        "kb_ingest_status",
    }
)

# Admin-only tools — never available to API keys regardless of tier or overrides
ADMIN_ONLY_TOOLS: frozenset[str] = frozenset({"kb_system_health", "kb_reprocess"})

_TIERS: dict[str, frozenset[str]] = {
    "search": SEARCH_TIER_TOOLS,
    "read": READ_TIER_TOOLS,
    "full": FULL_TIER_TOOLS,
}


def compute_effective_tools(tier: str, overrides: dict[str, bool]) -> frozenset[str]:
    """Compute the set of tools available to an API key.

    Starts with the tier's base set, applies overrides (true=add, false=remove),
    then strips admin-only tools.
    """
    base = _TIERS.get(tier, FULL_TIER_TOOLS)  # default to full for unknown tier
    effective = set(base)
    for tool_name, enabled in (overrides or {}).items():
        if enabled:
            effective.add(tool_name)
        else:
            effective.discard(tool_name)
    return frozenset(effective - ADMIN_ONLY_TOOLS)


@dataclass
class KeyScope:
    """Snapshot of an API key's scope, attached to Principal at auth time.

    Avoids per-tool-call DB lookups for scope info.
    """

    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    max_snippet_chars: int | None

    @property
    def is_unrestricted(self) -> bool:
        """True if no document-level scope filter applies."""
        return self.scope_topic_ids is None and self.scope_folder_ids is None

    @property
    def effective_tools(self) -> frozenset[str]:
        """The set of tool names this key may call."""
        return compute_effective_tools(self.permission_tier, self.tool_overrides)
