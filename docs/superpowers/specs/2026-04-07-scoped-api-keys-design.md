# Scoped API Keys — Design Spec

**Date:** 2026-04-07
**Status:** Draft
**Scope:** API key access control for external agent use cases

## Overview

Scoped API keys let admins create API keys with fine-grained access controls: which documents an agent can see, which MCP tools it can use, how much text it can read per call, and when the key expires. The primary use case is giving external agentic harnesses (OpenClaw, Claude, ChatGPT, etc.) controlled read access to a subset of the corpus without exposing everything.

All existing keys retain full access (NULL scopes = no restrictions). Scoping is opt-in per key.

## Goals

- Per-key document visibility scoping (by topic and/or watched folder)
- Per-key MCP tool permissions (tier-based with per-tool overrides)
- Per-key expiry dates
- Per-key snippet size limits
- Hard privacy boundary: scoped-out documents are completely invisible (not "access denied" — nonexistent)
- Silent tool omission: unavailable tools don't appear in the MCP tool list

## Non-Goals

- Per-key rate limiting (separate feature, Task #95)
- Per-key audit dashboard (separate feature, Task #94)
- Tag/label-based scoping (future: Projects/Collections, Task #97)
- Write access for API keys (keys remain read-only)

---

## Data Model

### Modified Table: `api_keys`

| Column | Type | Notes |
|---|---|---|
| `expires_at` | DateTime(tz) NULL | Optional expiry. Auth rejects after this time. NULL = never expires. |
| `permission_tier` | Text DEFAULT 'full' | `search`, `read`, or `full` |
| `tool_overrides` | JSONB DEFAULT '{}' | Per-tool on/off relative to tier, e.g. `{"kb_read_document": false}` |
| `scope_topic_ids` | JSONB DEFAULT NULL | Array of topic IDs. NULL = all topics (no restriction). |
| `scope_folder_ids` | JSONB DEFAULT NULL | Array of watched folder UUIDs. NULL = no folder restriction. |
| `max_snippet_chars` | Integer NULL | Per-passage char limit on read tools. NULL = no restriction. |

All new columns are nullable with permissive defaults, so existing keys are unaffected.

### Permission Tiers

Each tier defines a base set of MCP tools. `tool_overrides` can flip individual tools on or off relative to the tier.

| Tier | Tools Included |
|---|---|
| `search` | `kb_search`, `kb_batch_search`, `kb_corpus_overview`, `kb_list_recent` |
| `read` | All `search` tools + `kb_read_passages`, `kb_expand_context`, `kb_document_outline`, `kb_get_document` |
| `full` | All `read` tools + `kb_read_document`, `kb_find_related`, `kb_entity_search`, `kb_entity_overview`, `kb_entity_cooccurrence`, `kb_ingest_status` |

**Admin-only tools** (`kb_system_health`, `kb_reprocess`) are excluded from all tiers. They require admin role at the implementation level and are never available to API keys regardless of tier or overrides. `tool_overrides` with `true` for admin-only tools is silently ignored.

**Effective tool set** = tier base tools, then apply overrides (true = add, false = remove). Overrides cannot grant access to admin-only tools.

### Document Scoping Logic

A document is visible to a scoped key if:

1. `scope_topic_ids` is NULL **AND** `scope_folder_ids` is NULL → all documents visible
2. Otherwise, document is visible if:
   - `scope_topic_ids` is not NULL and document's `topic_id` is in the list, **OR**
   - `scope_folder_ids` is not NULL and document has a `watched_files` entry with `folder_id` in the list

When both scopes are set, they combine with **OR** — a document matching either scope is visible. This avoids the confusing case where a document must be in both an allowed topic and an allowed folder.

---

## Auth Enforcement

### Expiry Check

In both `get_current_principal()` (deps.py) and `_resolve_principal()` (mcp_server.py), after finding the API key row and confirming `is_active`:

```python
if api_key.expires_at and api_key.expires_at < datetime.now(UTC):
    raise HTTPException(401, "API key expired")
```

### Tool Gating — Silent Omission

Tools outside the key's effective set should not appear in the MCP tool listing. The agent never sees them — no error, no hint they exist.

**Implementation approach:** The MCP server uses FastMCP's static `@mcp.tool()` decorator, which registers all 16 tools globally at module load time. Per-session filtering requires intercepting the `tools/list` response. Two options:

1. **Filter at `list_tools` response** — wrap or hook the FastMCP `list_tools` handler to remove tools not in the key's effective set before returning to the client. Tool call attempts to filtered tools return a standard "unknown tool" MCP error (indistinguishable from the tool not existing).

2. **Per-key FastMCP instance** — create a new FastMCP app per key with only the allowed tools registered. Too expensive (16 decorator registrations per request).

**Chosen: option 1.** Add middleware/hook in the MCP ASGI layer that:
- On `tools/list` requests: filter the response to only include tools in the key's effective set
- On `tools/call` requests: if the tool is not in the effective set, return MCP error "unknown tool"

The effective tool set is computed from `principal.key_scope.permission_tier` + `principal.key_scope.tool_overrides` at auth time and cached on the Principal. Admin-only tools (`kb_system_health`, `kb_reprocess`) are always excluded for API key principals regardless of overrides.

### Document Scoping — `apply_key_scope()`

#### Carrying scope on Principal (avoid double DB lookup)

The `_resolve_principal()` function already fetches the full `ApiKey` row during auth. Instead of discarding the scope fields and re-fetching later, extend `Principal` to carry them:

```python
@dataclass
class KeyScope:
    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    max_snippet_chars: int | None

@dataclass
class Principal:
    type: str
    id: uuid.UUID
    role: str
    key_scope: KeyScope | None = None  # populated for api_key principals
```

Populated at auth time in `get_current_principal()` and `_resolve_principal()`. No second DB round-trip on tool calls.

#### `apply_key_scope()` helper

A query-modifying function (async, matching the codebase's async session pattern):

```python
def apply_key_scope(query: Select, principal: Principal) -> Select:
    """Filter query to only documents visible to this API key's scopes.
    Pure query builder — no DB access needed (scope loaded at auth time).
    """
    if principal.type != "api_key" or principal.key_scope is None:
        return query

    scope = principal.key_scope
    if scope.scope_topic_ids is None and scope.scope_folder_ids is None:
        return query  # no restrictions

    conditions = []
    if scope.scope_topic_ids is not None:
        conditions.append(Document.topic_id.in_(scope.scope_topic_ids))
    if scope.scope_folder_ids is not None:
        folder_uuids = [uuid.UUID(fid) for fid in scope.scope_folder_ids]
        watched_doc_ids = select(WatchedFile.doc_id).where(
            WatchedFile.folder_id.in_(folder_uuids),
            WatchedFile.status == WatchedFileStatus.active,
        )
        conditions.append(Document.doc_id.in_(watched_doc_ids))

    return query.where(or_(*conditions))
```

This is a pure query builder (no DB access) since scope data is on the Principal. Called in every code path that returns documents to API key consumers:
- MCP tools: `kb_search`, `kb_batch_search`, `kb_read_passages`, `kb_expand_context`, `kb_get_document`, `kb_list_recent`, `kb_corpus_overview`, `kb_document_outline`, `kb_find_related`, `kb_entity_search`, `kb_entity_overview`, `kb_entity_cooccurrence`, `kb_read_document`
- REST endpoints: `GET /api/docs`, `GET /api/docs/{id}`, `GET /api/search`

#### Chunk-level scope enforcement

Tools that fetch by chunk ID (`kb_read_passages`, `kb_expand_context`) bypass document-level query filtering. After fetching chunks, add an explicit doc_id membership check:

```python
if principal.key_scope and (principal.key_scope.scope_topic_ids or principal.key_scope.scope_folder_ids):
    visible_doc_ids = {row.doc_id for row in session.execute(
        apply_key_scope(select(Document.doc_id), principal)
    ).all()}
    chunks = [c for c in chunks if c.doc_id in visible_doc_ids]
```

### Snippet Size Enforcement

In `kb_read_passages` and `kb_expand_context` MCP tool implementations, after retrieving passage text:

```python
if principal.key_scope and principal.key_scope.max_snippet_chars is not None:
    passage.text = passage.text[:principal.key_scope.max_snippet_chars]
```

Note: uses `is not None` (not truthiness) so `max_snippet_chars=0` correctly returns empty text.

Applied per passage, not per response — so an agent requesting 10 passages each gets truncated individually.

---

## API Changes

All endpoints under `/api/api-keys` require `require_admin`.

### `POST /api/api-keys` — Create Key (extended)

**Request body:**

```python
class CreateApiKeyRequest(BaseModel):
    name: str
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] = "full"
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None
```

**Response:** unchanged — returns raw_key, key_id, name, mcp_path, created_at.

### `PATCH /api/api-keys/{key_id}` — Edit Scopes (new)

Update scopes on an existing key without regenerating the secret.

**Request body:**

```python
class PatchApiKeyRequest(BaseModel):
    name: str | None = None
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] | None = None
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None
```

**Clearing scopes (setting back to "no restriction"):** Use Pydantic v2's `model_fields_set` to distinguish "field absent" (no change) from "field explicitly set to null" (clear). Implementation:

```python
patch = body.model_dump(exclude_unset=True)
for field, value in patch.items():
    setattr(api_key, field, value)  # None = clear the scope
```

Fields not sent in the JSON body are excluded from `patch` and left unchanged. Fields sent as `null` are included with `None` value and clear the scope. This is the standard Pydantic v2 partial update pattern.

**Response:** updated key metadata (same as GET list item).

### `GET /api/api-keys` — List Keys (enriched)

Response items gain new fields:

```python
class ApiKeyOut(BaseModel):
    key_id: str
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    max_snippet_chars: int | None
    scope_summary: str  # computed: "Full access", "3 topics, 1 folder", etc.
```

### `GET /api/api-keys/{key_id}/scope-preview` — Preview Accessible Documents (new)

Returns the count of documents visible under this key's current scopes.

**Response:**

```python
class ScopePreviewResponse(BaseModel):
    accessible_documents: int
    total_documents: int
```

Useful for the admin to verify scoping before handing out a key.

---

## Frontend — API Keys Page

### Create Key Form

Expanded from just a name field:

- **Name** (text input, required)
- **Expires** (date picker or "Never" toggle, optional)
- **Permission tier** (radio group: Search / Read / Full, default Full)
- **Disclosure: "Tool overrides"** — collapsible section containing a grid of tool checkboxes, pre-filled based on selected tier. User can toggle individual tools on/off.
- **Disclosure: "Document scope"** — collapsible section with:
  - **Topics** multi-select (fetched from `/api/stats/topics` or similar). "All topics" when empty.
  - **Watched folders** multi-select (fetched from `/api/watch/folders`). "All folders" when empty.
- **Max snippet size** (number input, placeholder "No limit", optional)

### Key List Table

Existing columns (Name, Status, Created, Last Used, Actions) plus:

- **Scope** — compact summary: "Full access", "Search only", "3 topics, expires Apr 30", etc.
- **Expires** — date or "Never"

### Edit Panel

Clicking a key row opens a detail panel or modal with the same form fields (except name is editable, raw key is not shown). Pre-populated from current values. Saves via `PATCH /api/api-keys/{key_id}`.

### Scope Preview

After setting/changing scopes, a line below the scope section shows: "This key can access N of M documents" (fetched from scope-preview endpoint on scope change, debounced).

---

## Migration

Single Alembic migration adding columns to `api_keys`:

- `expires_at` (DateTime(tz), nullable)
- `permission_tier` (Text, NOT NULL, server_default='full')
- `tool_overrides` (JSONB, NOT NULL, server_default='{}')
- `scope_topic_ids` (JSONB, nullable)
- `scope_folder_ids` (JSONB, nullable)
- `max_snippet_chars` (Integer, nullable)

All existing keys get defaults = full unrestricted access. No data migration needed.

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Key with expired `expires_at` | Auth returns 401 "API key expired" — same as invalid key from agent's perspective |
| Key scoped to topic that gets deleted | Documents lose their `topic_id` on recompute → fall out of scope. Key sees fewer docs. Admin should update scope. |
| Key scoped to watched folder that gets removed | Folder deletion hard-deletes documents → they disappear from scope naturally. |
| Key with empty arrays `[]` for both scopes | No documents visible. Effectively a dead key. |
| Key with `scope_topic_ids: []` and `scope_folder_ids: null` | No topic matches, no folder restriction → no documents visible (OR of empty + null = empty). |
| Key with `scope_topic_ids: null` and `scope_folder_ids: [uuid]` | Only documents in that folder. NULL on one axis means "don't restrict on this axis" — the other axis still applies. |
| `tool_overrides` references unknown tool | Ignored silently. |
| `max_snippet_chars` set to 0 | Passages returned empty. Unusual but valid — agent can search but not read content. |
| Admin edits key scopes while agent is mid-session | Next MCP tool call uses new scopes. No session invalidation needed. |
| `kb_corpus_overview` with scoped key | Returns overview of only visible documents — counts, languages, types all reflect the scoped subset. |

**Clarification on scope_topic_ids: null + scope_folder_ids: [uuid]:**

The OR logic means: if either scope is NULL (no restriction on that axis), the NULL axis doesn't contribute a filter. So:
- `topic_ids: null, folder_ids: null` → no filter (all docs)
- `topic_ids: [1,2], folder_ids: null` → docs in topic 1 or 2 only
- `topic_ids: null, folder_ids: [uuid]` → docs in that folder only
- `topic_ids: [1,2], folder_ids: [uuid]` → docs in topic 1 or 2 OR in that folder

The NULL means "don't restrict on this axis" but the other axis still applies. If you want "all documents", leave both NULL.

---

## Testing

**Python (pytest):**
- Expiry: key with past expires_at returns 401
- Expiry: key with future expires_at succeeds
- Tier filtering: search-tier key can't use kb_read_passages
- Tool overrides: full-tier key with `{"kb_reprocess": false}` can't reprocess
- Document scoping: key scoped to topic 1 can't see topic 2 documents
- Document scoping: key scoped to folder A can't see folder B documents
- OR logic: key scoped to topic 1 + folder A sees both sets
- Snippet truncation: key with max_snippet_chars=100 gets truncated passages
- Scope preview: returns correct counts for scoped vs unscoped keys
- PATCH: editing scopes takes effect on next request
- Existing keys (no scopes): full access preserved

**Manual:**
- Create a scoped key in the UI, verify disclosure triangles work
- Hand scoped key to Claude Desktop via MCP, verify it only sees scoped documents
- Edit scopes on existing key, verify changes take effect
- Create key with expiry, wait for expiry, verify auth fails
