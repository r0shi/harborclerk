# Per-API-Key Audit Dashboard

## Goal

Give admins full introspection into how each API key is being used — which tools are called, what data is accessed, what gets denied, and when. Security/compliance is the primary lens ("did the ChatGPT connector access finance documents last week?"), with operational visibility and quota planning as natural by-products.

## Design Principle

Maximum introspection available when wanted, hidden when not. Every piece of telemetry should be accessible but never forced on the user.

## Data Layer

### New table: `api_request_log` (migration 0014)

| Column | Type | Notes |
|---|---|---|
| `request_id` | UUID PK | auto-generated |
| `api_key_id` | UUID FK → api_keys | SET NULL on delete (preserve orphan history) |
| `request_type` | text NOT NULL | `"mcp_tool"` or `"rest"` |
| `endpoint` | text NOT NULL | tool name (`kb_search`) or REST method+path (`GET /api/docs/{id}`) |
| `parameters` | JSONB | tool args or query params (no secrets, no request bodies) |
| `status` | text NOT NULL | `"ok"`, `"error"`, `"denied"` |
| `status_detail` | text, nullable | error message or denial reason |
| `result_summary` | JSONB, nullable | e.g. `{"count": 8}` or `{"count": 0, "would_match_unscoped": 3}` |
| `duration_ms` | integer | wall-clock request time |
| `created_at` | timestamptz NOT NULL | server default `now()` |

**Indexes:**
- `(api_key_id, created_at DESC, endpoint)` — per-key timeline + tool aggregation (covers `top_tools` GROUP BY without re-scanning)
- `(created_at)` — reaper cleanup
- `(endpoint)` — cross-key filter/aggregate by tool or REST path

**Retention:** 90-day auto-purge added to `_session_reaper_loop` in `src/harbor_clerk/api/app.py` (the existing background reaper). Runs alongside the current session/research cleanup cycle. Orphan rows (where `api_key_id` is NULL from a deleted key) are covered by the time-based `DELETE WHERE created_at < now() - interval '90 days'` — no special handling needed.

**Why not reuse `audit_log`:** `audit_log` tracks admin CRUD actions (create key, delete document). `api_request_log` tracks runtime request telemetry. Different schemas, different query patterns, different retention needs. Mixing them complicates both.

## Instrumentation Layer

Two logging points, both writing to `api_request_log` via a shared `log_api_request()` async helper. Logging is **awaited** (not fire-and-forget) — a single INSERT is ~1ms and avoids task lifecycle issues in the MCP stateless HTTP transport where `asyncio.create_task` tasks can be cancelled when the handler scope exits.

### 1. MCP tool calls

Instrumented in `ScopedFastMCP.call_tool()` — the single dispatch point for all MCP tool invocations.

- **Before:** capture start time, tool name, arguments
- **After:** capture duration, result summary, status
- **On denial** (tool not in `effective_tools`): log with `status="denied"`, `status_detail` explaining why (e.g. "tool not in search tier")

**Scope-filtered empty result detection:** Implemented inside `kb_search` and `kb_batch_search` tool functions (not in `call_tool`, which only sees opaque `CallToolResult` objects). When a scoped search returns 0 results, these tools fire one extra unscoped count query and include `would_match_unscoped` in the returned JSON. The `call_tool` instrumentation then picks this up from the result content for the log's `result_summary`. This only applies to search tools — other tools don't have a meaningful "scope filtered something" signal.

### 2. REST API requests

Lightweight FastAPI middleware that fires only for requests authenticated with an API key (skips human JWT users). Detects API key principal via the existing `Principal` dependency.

- Method + normalized path. **Normalization rule:** replace any UUID-shaped path segment (`[0-9a-f]{8}-...`) with `{id}`, producing e.g. `GET /api/docs/{id}` instead of `GET /api/docs/550e8400-...`
- Duration, response status code mapped to ok/error
- No request body logging (too large for uploads, privacy concern)

## API Endpoints

All admin-only, scoped to a single key by path param.

### `GET /api/api-keys/{key_id}/usage`

Summary stats for the dashboard cards. Returns all time buckets in one response so dropdown switching is instant (no round-trips).

```json
{
  "requests": {"1h": 12, "6h": 45, "12h": 89, "24h": 150, "7d": 980, "30d": 4200},
  "errors": {"1h": 0, "24h": 2, "7d": 5},
  "denials": {"1h": 0, "24h": 0, "7d": 1},
  "last_used_at": "2026-04-10T12:34:56Z",
  "top_tools": {
    "1h":  [{"endpoint": "kb_search", "count": 8}],
    "6h":  [{"endpoint": "kb_search", "count": 30}],
    "12h": [{"endpoint": "kb_search", "count": 55}],
    "24h": [{"endpoint": "kb_search", "count": 90}],
    "7d":  [{"endpoint": "kb_search", "count": 600}],
    "30d": [{"endpoint": "kb_search", "count": 2500}]
  }
}
```

`last_used_at` is `MAX(created_at)` from `api_request_log` for this key — consistent with what the dashboard measures, rather than `api_keys.last_used_at` which also fires on auth-only requests.

### `GET /api/api-keys/{key_id}/usage/timeline`

Daily request counts for the timeline chart.

- Query params: `days` (default 30, max 90, validated via `Field(ge=1, le=90)`)
- Response: `[{"date": "2026-04-10", "ok": 45, "error": 2, "denied": 0}, ...]`

### `GET /api/api-keys/{key_id}/usage/requests`

Server-side paginated request log table.

- Query params: `page` (default 1), `page_size` (default 25, max 200), `endpoint` (filter), `status` (filter), `from`/`to` (date range)
- Response: `{"items": [RequestLogEntry], "total": int, "page": int, "page_size": int}`

Each `RequestLogEntry`:
```json
{
  "request_id": "uuid",
  "request_type": "mcp_tool",
  "endpoint": "kb_search",
  "parameters": {"query": "budget 2025", "k": 10},
  "status": "ok",
  "status_detail": null,
  "result_summary": {"count": 8},
  "duration_ms": 145,
  "created_at": "2026-04-10T12:34:56Z"
}
```

### `DELETE /api/api-keys/{key_id}/usage`

Purge all logged events for this key. Two-click confirm in the UI (WKWebView has no `window.confirm`).

- Response: `{"deleted": int}`

## Frontend

### Navigation

Key name in the API Keys table becomes a link to `/admin/api-keys/:keyId`. New route handled by `ApiKeyDashboardPage.tsx`.

### Page layout

**Header bar:**
- Key name, tier badge, scope summary, active/revoked status
- Back link to API Keys list
- Purge button (two-click confirm pattern)

**Summary cards row:**
- **Requests** — single card with count + dropdown selector: 1h / 6h / 12h / 24h / 7d / 30d
- **Errors** — single card with count + dropdown selector: 1h / 24h / 7d. Red-tinted if > 0.
- **Denials** — single card with count + dropdown selector: 1h / 24h / 7d. Amber-tinted if > 0.
- **Last used** — timestamp, no selector needed

**Charts section (recharts, already a project dependency):**
- **Requests over time** — daily stacked bars (ok / error / denied), configurable range
- **Tool breakdown** — horizontal bar chart, top tools by call count. Same dropdown range as Requests card: 1h / 6h / 12h / 24h / 7d / 30d

**Request log table:**
- Columns: Time, Endpoint, Parameters (truncated), Status (badge), Duration, Result summary
- Filters: endpoint dropdown, status dropdown, date range picker
- Server-side pagination with page size selector (25 / 50 / 100 / 200)
- Expandable rows: click to reveal full parameters and result detail
- Error/denial rows: red/amber left border for scannability without filtering

## Files

| File | Change |
|---|---|
| `alembic/versions/0014_api_request_log.py` | New migration |
| `src/harbor_clerk/models/api_request_log.py` | New model |
| `src/harbor_clerk/api/request_log.py` | `log_api_request()` async helper |
| `src/harbor_clerk/mcp_server.py` | Instrument `ScopedFastMCP.call_tool()` with timing + logging |
| `src/harbor_clerk/mcp_server.py` | `kb_search`/`kb_batch_search`: add `would_match_unscoped` on empty scoped results |
| `src/harbor_clerk/api/middleware.py` | New: REST request logging middleware (API key requests only) |
| `src/harbor_clerk/api/routes/api_keys.py` | 4 new endpoints (usage, timeline, requests, purge) |
| `src/harbor_clerk/api/schemas/api_keys.py` | Response schemas for new endpoints |
| `src/harbor_clerk/api/app.py` | Add 90-day purge to `_session_reaper_loop` |
| `frontend/src/pages/ApiKeyDashboardPage.tsx` | New page |
| `frontend/src/pages/ApiKeysPage.tsx` | Key name becomes link |
| `frontend/src/App.tsx` | Add route `/admin/api-keys/:keyId` |
