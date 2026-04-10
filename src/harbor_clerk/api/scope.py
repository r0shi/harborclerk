"""Per-API-key scope computation: tiers, tool sets, and query filters."""

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

if TYPE_CHECKING:
    from harbor_clerk.api.deps import Principal

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
        """True if no document-level scope filter applies.

        Both None and [] mean "no restriction" for each axis.
        """
        return not self.scope_topic_ids and not self.scope_folder_ids

    @property
    def effective_tools(self) -> frozenset[str]:
        """The set of tool names this key may call."""
        return compute_effective_tools(self.permission_tier, self.tool_overrides)


def apply_key_scope(query: Select, principal: "Principal") -> Select:
    """Filter a Document query by the API key's scope.

    No-op for human users (type='user') and unrestricted API keys.
    For scoped keys, adds a WHERE clause filtering documents to those
    matching the topic OR folder scope.

    Pure query builder — no DB access (scope is on Principal from auth time).
    """
    if principal.type != "api_key" or principal.key_scope is None:
        return query
    scope = principal.key_scope
    if scope.is_unrestricted:
        return query

    conditions = []
    if scope.scope_topic_ids:
        conditions.append(Document.topic_id.in_(scope.scope_topic_ids))
    if scope.scope_folder_ids:
        try:
            folder_uuids = [uuid.UUID(fid) for fid in scope.scope_folder_ids]
        except (ValueError, AttributeError):
            folder_uuids = []
        if folder_uuids:
            watched_doc_ids = select(WatchedFile.doc_id).where(
                WatchedFile.folder_id.in_(folder_uuids),
                WatchedFile.status == WatchedFileStatus.active,
            )
            conditions.append(Document.doc_id.in_(watched_doc_ids))

    if not conditions:
        # Both axes empty — explicitly nothing visible
        return query.where(Document.doc_id == uuid.UUID(int=0))

    return query.where(or_(*conditions))
