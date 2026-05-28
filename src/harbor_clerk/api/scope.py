"""Per-API-key scope computation: tiers, tool sets, and query filters."""

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

# Tier -> base tool set
SEARCH_TIER_TOOLS: frozenset[str] = frozenset(
    {
        "kb_search",
        "kb_batch_search",
        "kb_corpus_overview",
        "kb_list_recent",
        "kb_find_all",
        "kb_documents_by_date",
    }
)
READ_TIER_TOOLS: frozenset[str] = SEARCH_TIER_TOOLS | frozenset(
    {
        "kb_read_passages",
        "kb_expand_context",
        "kb_document_outline",
        "kb_get_document",
        "kb_verify_identifier",
    }
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
    rate_limit_rpm: int | None
    rate_limit_rph: int | None

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


@dataclass
class UserScope:
    """Per-request scope for human-user tool calls.

    Distinct from KeyScope: no permission_tier / tool_overrides / rate_limit —
    human users authenticate via JWT and aren't tool-gated. Only document-axis
    filters live here. Forward-compatible: collection_ids, doc_ids, topic_ids
    will be added here additively when Collections ships.
    """

    folder_ids: list[uuid.UUID] | None = None

    @property
    def is_unrestricted(self) -> bool:
        """True when no document-axis filter applies. Both None and [] count."""
        return not self.folder_ids


class _ScopedPrincipal(Protocol):
    """Subset of Principal that apply_key_scope reads.

    Defined as a Protocol so apply_key_scope doesn't need to import the
    full Principal from deps — avoiding the scope ⇄ deps cyclic import.
    Principal in deps satisfies this protocol structurally.
    """

    type: str
    key_scope: KeyScope | None


def build_user_scope(scope_dict: dict | None) -> "UserScope | None":
    """Build a UserScope from a Conversation.scope or ResearchState.scope JSONB dict.

    Returns None when scope_dict is None, empty, or contains an empty folder_ids list.
    String UUIDs are parsed to uuid.UUID; UUID objects pass through unchanged.
    """
    if not scope_dict:
        return None
    folder_ids_raw = scope_dict.get("folder_ids") or []
    if not folder_ids_raw:
        return None
    folder_ids = [uuid.UUID(fid) if isinstance(fid, str) else fid for fid in folder_ids_raw]
    return UserScope(folder_ids=folder_ids)


def _folder_watched_doc_ids_subquery(folder_ids: list[uuid.UUID]) -> Select:
    """Subquery yielding doc_ids that have an ACTIVE WatchedFile in any of `folder_ids`.

    Shared by apply_folder_scope and apply_key_scope's folder branch so both call sites
    use the same SQL shape.
    """
    return select(WatchedFile.doc_id).where(
        WatchedFile.folder_id.in_(folder_ids),
        WatchedFile.status == WatchedFileStatus.active,
    )


def apply_folder_scope(query: Select, folder_ids: list[uuid.UUID] | None) -> Select:
    """Filter a Document query to documents whose active WatchedFile lives in any
    of the given folders. No-op when folder_ids is None or empty.

    Pure query builder — no DB access. Safe to compose with other WHERE clauses.
    """
    if not folder_ids:
        return query
    return query.where(Document.doc_id.in_(_folder_watched_doc_ids_subquery(folder_ids)))


def apply_key_scope(query: Select, principal: _ScopedPrincipal) -> Select:
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
            conditions.append(Document.doc_id.in_(_folder_watched_doc_ids_subquery(folder_uuids)))

    if not conditions:
        # Both axes empty — explicitly nothing visible
        return query.where(Document.doc_id == uuid.UUID(int=0))

    return query.where(or_(*conditions))
