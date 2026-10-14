"""Per-API-key scope computation: tiers, tool sets, and query filters."""

import uuid
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel
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


def minimum_tier_for_tool(tool: str) -> str | None:
    """The lowest tier whose base set includes ``tool``; None for admin-only or unknown tools."""
    for tier in ("search", "read", "full"):
        if tool in _TIERS[tier]:
            return tier
    return None


# ---------------------------------------------------------------------------
# REST routes reachable by API keys
# ---------------------------------------------------------------------------

# Every REST route an API key may call, mapped to the MCP tool whose data it
# exposes. A key may call a route iff it may call that tool — tier plus per-key
# overrides, via ``compute_effective_tools`` — so REST and MCP enforce one
# contract and the admin UI's tool checklist describes both surfaces (#646).
# Where a route exposes data from several tools, the most privileged governs.
#
# Keys are (HTTP method, route template as FastAPI registers it: the router's
# own prefix included, the app-level ``/api`` mount excluded) — what
# ``request.scope["route"].path`` yields inside a dependency.
#
# A route that uses ``require_read_access`` but has no entry here is CLOSED to
# API keys; ``tests/test_rest_api_key_tier.py`` holds the two in step. Routes
# with no MCP counterpart — jobs, stats, watched folders, chat — are human-only
# and do not appear.
REST_ROUTE_TOOLS: dict[tuple[str, str], str] = {
    # search tier
    ("POST", "/search"): "kb_search",
    ("POST", "/search/find-all"): "kb_find_all",
    ("GET", "/docs"): "kb_list_recent",
    ("GET", "/docs/overview"): "kb_corpus_overview",
    ("GET", "/docs/filters"): "kb_corpus_overview",
    # read tier
    ("POST", "/passages/read"): "kb_read_passages",
    ("GET", "/docs/{doc_id}"): "kb_get_document",
    ("GET", "/docs/{doc_id}/outline"): "kb_document_outline",
    # full tier
    ("GET", "/docs/{doc_id}/content"): "kb_read_document",
    # Raw bytes are at least the full text; additionally gated by ALLOW_SOURCE_DOWNLOAD.
    ("GET", "/docs/{doc_id}/download"): "kb_read_document",
    ("GET", "/docs/{doc_id}/related"): "kb_find_related",
    ("GET", "/docs/{doc_id}/entities"): "kb_entity_overview",
    ("GET", "/docs/entities/autocomplete"): "kb_entity_search",
    ("GET", "/docs/entities/top"): "kb_entity_overview",
}


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


# Response fields that carry an absolute on-disk path. Human sessions keep them
# (the web app renders them and Reveal in Finder needs them); an API key never
# receives one, at any tier — MCP payloads have never carried them
# (tests/test_mcp_source_ref.py) and REST follows the same contract.
ABSOLUTE_PATH_FIELDS: frozenset[str] = frozenset({"source_path", "watch_source_path"})


def redact_paths_for_principal[Payload](payload: Payload, principal: _ScopedPrincipal) -> Payload:
    """Blank every ``ABSOLUTE_PATH_FIELDS`` member throughout ``payload`` for an API-key caller.

    Walks pydantic models, dicts, lists and tuples in place and returns ``payload``,
    so a route can wrap its return value. No-op for human principals.
    """
    if principal.type == "api_key":
        _blank_path_fields(payload)
    return payload


def _blank_path_fields(obj: object) -> None:
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            if name in ABSOLUTE_PATH_FIELDS:
                setattr(obj, name, None)
            else:
                _blank_path_fields(getattr(obj, name, None))
    elif isinstance(obj, dict):
        for key in list(obj):
            if key in ABSOLUTE_PATH_FIELDS:
                obj[key] = None
            else:
                _blank_path_fields(obj[key])
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _blank_path_fields(item)


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
