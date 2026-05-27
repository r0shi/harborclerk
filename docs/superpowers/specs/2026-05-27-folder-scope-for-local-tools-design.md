# Folder-Scope Filter for Local Tools — Design Spec

**Date:** 2026-05-27
**Status:** Draft
**Scope:** User-facing folder filter on Ask, Research, and Search. Closes the human-side of task #97 (Projects/Collections) without shipping saved/named collections.

## Overview

Today, only API keys can scope what documents an agent sees — via `KeyScope.scope_folder_ids` enforced by `apply_key_scope()` in `src/harbor_clerk/api/scope.py`. Human users have no equivalent: an Ask conversation always searches every active document; the same is true for Research and Search.

This spec adds a folder-level filter to the three user-driven tools (Ask, Research, Search), surfaced in the existing per-tool config UI and stored on each tool's natural container. The data model and API shape are wrapped to allow collections (future) without a tear-down.

This is **not** Projects/Collections (task #97). It is the folder-scope half of that work, shipped now to unblock users who want to ask about a specific subset of their corpus.

## Motivation

- The watched-folder-first refactor (PRs #245, #253, #255) deliberately deferred this filter so it could land alongside Projects/Collections. Five specs reference task #97 as the future home; no design exists for it.
- Users with mixed corpora (e.g. "contracts" + "personal scans" + "research papers" under one Harbor Clerk instance) currently can't tell Ask to look in just one slice. The whole-corpus default surfaces irrelevant hits and bleeds context across logical domains.
- The backend infrastructure already exists. The work is mostly UI placement, naming, and the small lift of threading the scope through the chat-tool dispatch path.

## Goals

- Each of Ask / Research / Search can be limited to a user-selected set of watched folders.
- Same filter semantics as API key scoping: empty selection = no restriction; selected folders combine with OR (any-in-set).
- Backend reuses one query helper across the API-key path and the user-driven path. Single source of truth.
- Forward-compatible API + DB shape so adding collections / doc-level inclusions later is purely additive — no migration, no endpoint version bump.

## Non-Goals

- **Saved / named scopes.** That's "Collections" — separate feature.
- **Topic-axis filtering for users.** `KeyScope.scope_topic_ids` already exists; can be added to the user surface in a future pass without redesign.
- **A global "active scope"** that follows you across tools. Each tool sets its own.
- **Sub-folder selection** (per-doc inclusion/exclusion within a folder). Folders are atomic in v1.
- **MCP enforcement of user-scope.** MCP keys have their own scope; the user-side filter does not bleed into MCP traffic.

---

## UX per surface

All three reuse one `<FolderPicker>` component: a multi-select with search-as-you-type, "Select all" / "Clear" affordances, and active-folder count. The picker fetches from `GET /api/watch/folders` (filtering to `unavailable_reason IS NULL` so removed/unmounted folders don't appear). Empty selection renders as **"Folders: All"** in any chip.

### Ask (chat)

- **Where the picker lives:** in the **New Conversation modal**, alongside the model selector. Once the conversation exists, the picker is gone — the conversation header shows a read-only "Folders: All" (or "Folders: Contracts, Legal (2)") chip so the active scope is always visible, but it isn't editable.
- **When it's set:** at conversation creation. Default: empty (= all).
- **Lifecycle:** **immutable for the conversation.** To use a different scope, start a new conversation. This matches Research and keeps the mental model simple: each conversation is one scope's worth of context.
- **Persistence:** `conversations.scope` (new JSONB column).

### Research

- **Where the picker lives:** in the start-research form, in the same row as strategy / depth / time limit.
- **When it's set:** at research start. **Immutable for the run** — same lifecycle as the other run params.
- **Persistence:** `research_state.scope` (new JSONB column).
- **Visibility in research detail/history:** scope is shown alongside other run config (model, strategy, depth, time limit) so completed runs are reproducible/auditable.

### Search

- **Where the picker lives:** a filter chip on the existing search filter row, next to language / MIME type / date range.
- **When it's set:** local React state on the search page. Changes apply on the next query (consistent with how the other filters behave).
- **Persistence:** none. Per-page-session only. Reload = back to empty.
- **Backend:** the existing `POST /api/search` body gains a `scope` field; if absent, no filter.

### Empty-state semantics

Both `null` (column unset) and `[]` (column explicitly empty) mean **no restriction**. Mirrors `KeyScope` exactly. The UI never represents "explicitly empty" differently from "unset" — both render as "Folders: All."

This implies an oddity worth flagging: a user cannot configure a chat conversation to mean "show me nothing." That's correct — there's no use case for a deliberately blank conversation, and the API key spec has the same property (`scope_topic_ids: [] AND scope_folder_ids: null` is the only way to get a dead key, and it's explicitly an edge case there).

---

## Data model

### New column on `conversations`

```sql
ALTER TABLE conversations
  ADD COLUMN scope JSONB NOT NULL DEFAULT '{}'::jsonb;
```

- **Default `{}`** (empty object), not `null`. Matches the "wrap in an object for forward-compat" rule.
- Today's only key is `folder_ids` (a list of folder UUIDs as strings). When collections arrive, additional keys join the same object — no migration.

### New column on `research_state`

```sql
ALTER TABLE research_state
  ADD COLUMN scope JSONB NOT NULL DEFAULT '{}'::jsonb;
```

Same shape, same forward-compat rules.

### No new tables

No `collections` table, no `saved_scopes` table, no join tables. Those land with the real Collections feature.

### Why a wrapper object instead of a bare `scope_folder_ids` column

The API key columns (`scope_folder_ids JSONB`, `scope_topic_ids JSONB`) work because they were defined together at once. Adding `scope_collection_ids` later means a new column and a new endpoint field and a new tool-result handler. By wrapping in `scope: {folder_ids: [...]}` from day one, the same column accepts new axes purely as new JSON keys, with no DDL change.

The cost today is one extra layer of indirection (`scope.folder_ids` instead of `scope_folder_ids`). The benefit is open-ended additive growth.

---

## API shape

### Request bodies (all new fields optional)

**`POST /api/chat/conversations`** — when creating a conversation:

```python
class CreateConversationRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    scope: ScopeSpec | None = None     # new
```

Scope is set once at creation. There is intentionally no PATCH endpoint — to use a different scope, start a new conversation.

**`POST /api/research`** — when starting a research run:

```python
class StartResearchRequest(BaseModel):
    query: str
    strategy: Literal["search_driven", "systematic"]
    depth: Literal["light", "standard", "thorough"]
    time_limit_minutes: int
    scope: ScopeSpec | None = None     # new
```

**`POST /api/search`** — per-request only:

```python
class SearchRequest(BaseModel):
    query: str
    # ... existing filter fields (language, mime_type, after, before, etc.)
    scope: ScopeSpec | None = None     # new
```

### The `ScopeSpec` schema

```python
class ScopeSpec(BaseModel):
    folder_ids: list[uuid.UUID] | None = None
    # Future fields (additive):
    # collection_ids: list[uuid.UUID] | None = None
    # doc_ids: list[uuid.UUID] | None = None
    # topic_ids: list[int] | None = None

    model_config = ConfigDict(extra="ignore")   # tolerate future keys on older clients
```

`extra="ignore"` matters: if a future client sends `collection_ids` to a server that doesn't know about them yet, the request still parses (silently ignoring the unknown key). Avoids version-coupling between client and server.

### Validation

- `folder_ids` is validated against existing watched folders on write. Unknown UUIDs reject with 422.
- `folder_ids` of folders with `unavailable_reason IS NOT NULL` (unmounted on Docker, missing on macOS) reject with 422 — don't let a user pin a scope to a folder that can't return docs.
- Empty array `[]` and absent field both serialize to the same stored form (`scope = {}` or `scope = {folder_ids: []}`). The filter helper treats both as no-restriction.

### Response surfaces

- `GET /api/chat/conversations/{id}` returns the current `scope` so the UI can render the chip.
- `GET /api/research/{id}` returns the run's `scope` for the detail view.
- `POST /api/search` response is unchanged — search results don't echo the scope back.

---

## Backend

### Filter helper extraction

Pull a pure query builder out of `apply_key_scope()`:

```python
# in src/harbor_clerk/api/scope.py

def apply_folder_scope(query: Select, folder_ids: list[uuid.UUID] | None) -> Select:
    """Filter a Document query to documents in any of the given folders.

    No-op when folder_ids is None or empty.
    """
    if not folder_ids:
        return query
    watched_doc_ids = select(WatchedFile.doc_id).where(
        WatchedFile.folder_id.in_(folder_ids),
        WatchedFile.status == WatchedFileStatus.active,
    )
    return query.where(Document.doc_id.in_(watched_doc_ids))
```

Then `apply_key_scope` becomes a thin caller (with topic axis OR'd in as today), and the new user-side path also calls `apply_folder_scope` directly.

### `UserScope` on Principal

Add a parallel dataclass next to `KeyScope`:

```python
@dataclass
class UserScope:
    """Per-request scope for human-user tool calls.

    Distinct from KeyScope: no permission_tier / tool_overrides / rate_limit —
    human users authenticate via JWT and aren't tool-gated. Only document-axis
    filters live here.
    """
    folder_ids: list[uuid.UUID] | None = None
    # Future fields mirror ScopeSpec (collection_ids, etc.)


@dataclass
class Principal:
    type: str
    id: uuid.UUID
    role: str
    key_scope: KeyScope | None = None
    user_scope: UserScope | None = None   # new
```

The MCP server's `_mcp_principal` ContextVar already carries Principal across the chat-tool path. Adding `user_scope` is mechanically the same pattern as `key_scope`.

### Threading scope through chat-tool dispatch

`execute_tool()` in `src/harbor_clerk/llm/tools.py` currently sets:

```python
principal = Principal(type="user", id=user_id, role="user")
token = _mcp_principal.set(principal)
```

Extend it to accept and forward a scope:

```python
async def execute_tool(
    name: str,
    arguments: dict,
    user_id: uuid.UUID | None = None,
    *,
    mode: str = "chat",
    user_scope: UserScope | None = None,   # new
) -> str:
    ...
    principal = Principal(
        type="user",
        id=user_id,
        role="user",
        user_scope=user_scope,
    )
    token = _mcp_principal.set(principal)
```

Inside each MCP tool body, the existing principal-scope hooks (which today only check `key_scope`) gain a parallel `user_scope` branch:

```python
# Before today:
if principal.key_scope and not principal.key_scope.is_unrestricted:
    query = apply_key_scope(query, principal)

# After:
if principal.key_scope and not principal.key_scope.is_unrestricted:
    query = apply_key_scope(query, principal)
elif principal.user_scope and principal.user_scope.folder_ids:
    query = apply_folder_scope(query, principal.user_scope.folder_ids)
```

API keys and human users never share scope (different principal types), so the `elif` is unambiguous.

### Chunk-level enforcement (mirrors API key path)

Tools that fetch by chunk ID (`kb_read_passages`, `kb_expand_context`) bypass document-level query filtering today and post-filter using `apply_key_scope`. They get the same post-filter for `user_scope` — same pattern, identical code shape.

### Research and Search backend paths

Research's retrieval is in-process; `src/harbor_clerk/llm/research.py` already runs through `execute_tool()` for tool calls and through internal helpers for direct DB queries. Both paths get `user_scope` from the `research_state.scope` row loaded at run start, passed in alongside the existing strategy/depth params.

Search's `/api/search` route reads `scope` from the request body, constructs `UserScope(folder_ids=...)`, and calls `apply_folder_scope` directly in the hybrid-search query construction. No tool-dispatch indirection needed — search is a single SQL build, not a multi-tool agent loop.

---

## Frontend

### `<FolderPicker>` component

New shared component in `frontend/src/components/`. Props:

```ts
interface FolderPickerProps {
  value: string[];                    // selected folder UUIDs (empty = all)
  onChange: (folder_ids: string[]) => void;
  folders: Folder[];                  // pre-fetched list
  disabled?: boolean;
  size?: 'sm' | 'md';                 // sm for chips, md for forms
}
```

Renders:
- Chip-style trigger when collapsed: "Folders: All" or "Folders: Contracts, Legal (2)"
- Popover on open: search-as-you-type input, scrollable folder list with checkboxes, "Select all" / "Clear" buttons, "Apply" / "Cancel" footer.
- Empty folder list (corpus has zero watched folders): trigger shows "No folders to scope to" and is disabled.

The folder list comes from `useWatchedFolders()` — a new TanStack Query hook that wraps `GET /api/watch/folders`, filters out unavailable folders client-side, and is shared across all three surfaces (so picker open is instant if the data is cached from a prior page).

### Ask page integration

- **New Conversation modal:** add `<FolderPicker size="md">` below the model selector. On submit, include the selected `folder_ids` in `scope: {folder_ids: [...]}` on the POST.
- **Conversation header:** render a **read-only** scope chip next to the conversation title. It always renders (even when scope is empty / "Folders: All") so users see what scope is active. Clicking it does nothing — or shows a tooltip "Scope is set per-conversation; start a new conversation to change it." (Tooltip is nice-to-have; the chip alone is sufficient.)
- **Same `<FolderPicker>` is reused in the modal**; for the header chip, a separate `<ScopeChip>` component renders the same visual but with no popover.

### Research page integration

- **Start form:** `<FolderPicker size="md">` in the same row as strategy / depth / time limit. Submitted with `scope: {folder_ids: [...]}` on POST.
- **Research detail header:** display the scope as static text alongside the other run config — no edit (research is one-shot).
- **Research history list:** if scope is non-empty, show a small "Folders: 2" badge on the list row. Doesn't need to enumerate; clicking opens the detail view.

### Search page integration

- **Filter row:** `<FolderPicker size="sm">` adjacent to the existing language / MIME / date filters. State held by the page; submitted with `scope: {folder_ids: [...]}` on each search.
- **No persistence:** reload clears the filter (matches the other filters' behavior today).

---

## Edge cases

| Scenario | Behavior |
|---|---|
| User picks a folder, then the folder gets removed/unmounted | Picker re-fetch hides it; the conversation/run continues with the now-empty/reduced set. If all selected folders become unavailable, the scope effectively returns nothing — chat says "I couldn't find anything in the selected folders." No automatic scope-clearing. |
| User picks folders, then deletes them entirely (active → removed) | Same as above. Documents are gone; queries return zero rows in scope. |
| New folder appears after a conversation has scope set | The new folder is NOT auto-added. Scope is "the folders you picked," not "everything you would have picked at the time." To use the new folder, start a new conversation. |
| User sets scope on a conversation, then exports / shares the conversation | Out of scope for v1 — there's no conversation export today. When there is, scope is part of the metadata so the conversation is reproducible. |
| MCP key principal also has user_id set (hypothetical hybrid) | The key_scope branch wins (we check it first). Human-driven user_scope never combines with API-key scope. |
| Research resume on a run with scope set | Resume reads `research_state.scope`; the scope is the same as the original run. (Resuming with a different scope is not supported — it would change the meaning of the existing notes.) |
| Conversation created via the API (not the UI) with no `scope` field | Defaults to `scope = {}` = no restriction. Same as today. |
| Conversation created with `scope = {folder_ids: []}` | Same as no restriction (`folder_ids` empty list = "no folder restriction"). |
| Scope contains a `folder_ids` UUID for a folder the user doesn't own | N/A — single-tenant. All folders are visible to all admin/user roles. |

---

## Testing

### Python (pytest)

- `apply_folder_scope`: empty list → no-op; populated list → restricts via WatchedFile join.
- `apply_folder_scope`: includes `WatchedFile.status == active` filter (removed files don't bleed in).
- `execute_tool`: passes `user_scope` into Principal; tool body sees it.
- Conversation create with `scope.folder_ids`: row persists; subsequent tool calls in the conversation are filtered.
- Conversation create without `scope` field: defaults to `{}` = no restriction; tool calls unrestricted.
- Conversation create with `scope.folder_ids = []`: same as no restriction.
- Research start with `scope.folder_ids`: persists; tool dispatch during the run filters; resume sees same scope.
- `POST /api/search` with `scope.folder_ids`: results restricted; results match the equivalent `kb_search` call with `key_scope.scope_folder_ids` set.
- Reject scope referencing a non-existent folder UUID with 422.
- Reject scope referencing an unavailable folder with 422.
- API key path (`key_scope`) still works identically — no regression from the refactor that extracted `apply_folder_scope`.
- `ScopeSpec` with future keys (`collection_ids: [...]`): parses without error and is silently ignored (`extra="ignore"`).
- Chunk-level fetch (`kb_read_passages`) for a `user_scope`d conversation respects scope on the post-fetch filter, same as API key path.

### Frontend (vitest + RTL)

- `<FolderPicker>` renders empty trigger as "Folders: All".
- `<FolderPicker>` selection round-trips: select two folders → trigger shows "Folders: A, B (2)" → reopen picker → both are checked.
- "Select all" / "Clear" affordances behave as named.
- Unavailable folders are hidden from the list.
- Empty corpus state (no folders): trigger reads "No folders to scope to" and the picker is disabled.

### Manual

- Open Ask via "New Conversation," set scope to one folder, ask a question. Verify only docs from that folder are cited and the header chip reads "Folders: <name>".
- Start a second conversation with a different folder selected. Verify the two conversations are independent and each only cites docs from its own scope.
- Start a research run scoped to two folders. Verify the run's notes only reference docs from those folders.
- Search with a folder scope. Verify the result list is restricted. Clear the scope; results widen.
- Compare against the equivalent MCP-key behavior: scoping an API key to the same folders should produce the same result set as the user-driven scope.

---

## Forward-compat checkpoint — what shipping Collections looks like

When Collections (task #97 proper) ships later, this design supports it additively:

1. **New `collections` table** with `(id, name, owner, created_at)`. Membership via a join table `(collection_id, folder_id)` for v1 of collections; later, `(collection_id, doc_id)` for sub-folder inclusion.
2. **Add `collection_ids: list[uuid.UUID]` to `ScopeSpec`.** The new field is accepted on every endpoint that already accepts `scope` — no endpoint signature change. Older clients sending only `folder_ids` keep working.
3. **Add `apply_collection_scope(query, collection_ids)`** to `src/harbor_clerk/api/scope.py`. It resolves collection members and OR-merges with `folder_ids` and any other axes, mirroring `apply_key_scope`'s existing OR-across-axes pattern.
4. **Extend `<FolderPicker>` → `<ScopePicker>` (or `<SourcePicker>`)** that shows folders and collections side-by-side in the same popover. Folders can still be picked individually; picking a collection picks its members atomically.
5. **No migration on `conversations.scope` or `research_state.scope`.** Existing rows keep their `folder_ids` keys; new rows can add `collection_ids` alongside.

The naming pivot from "Folders" → "Sources" (or "Scope") in the UI happens at that point, when there's more than one kind of thing to pick. Until then, "Folders" is honest about what's there.

---

## Out of scope (explicit non-goals, restated)

- Saved / named scopes (= Collections proper).
- Topic-axis user filter (`UserScope.topic_ids` etc.).
- Sub-folder doc-level inclusion / exclusion within v1.
- Global "active scope" carrying across tools.
- Conversation export with scope metadata (out of scope until conversation export exists).
- Permission gating: in a multi-tenant or per-user-folder-ownership world, scope might intersect with ownership. Single-tenant today, so the filter is purely an organization aid, not an access control.

## References

- Existing scoping plumbing: `src/harbor_clerk/api/scope.py` (`apply_key_scope`, `KeyScope`).
- Original scoped-API-keys spec: `docs/superpowers/specs/2026-04-07-scoped-api-keys-design.md`.
- Watched-folder-first deferral notes: `docs/superpowers/specs/2026-04-30-watched-folder-first-stage-{1,2}-design.md`, `2026-05-01-watched-folder-first-stage-3-design.md`.
- Memory: `~/.claude/projects/-Users-alex-mcp-gateway/memory/project_watched_folder_first_arc.md` (entry on the deferred folder filter).
