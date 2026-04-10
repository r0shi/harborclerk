# Per-API-Key Rate Limits

## Goal

Protect against runaway AI agents that hammer the API in a loop. Circuit breaker, not billing system.

## Design Principle

Safe defaults out of the box. Every new key gets rate limits automatically. Admins override per-key when needed. Maximum introspection — rate-limited requests are logged and visible in the audit dashboard.

## Data Model

### System defaults

Two new fields stored in the `model_settings` table (same pattern as LLM model config — DB-backed, runtime-editable, falls back to env var defaults):

- `default_rate_limit_rpm` — requests per minute, default `60`
- `default_rate_limit_rph` — requests per hour, default `2000`

Cached in memory; refreshed on PATCH. Exposed in System Settings UI via `GET/PATCH /api/system/settings`. In the macOS app, also synced to `native_config_file` via the existing `sync_native_config()` pattern.

### Per-key overrides (migration 0015)

Two new nullable columns on `api_keys`:

| Column | Type | Notes |
|---|---|---|
| `rate_limit_rpm` | integer, nullable | NULL = use system default. 0 = unlimited. >=1 = custom limit. |
| `rate_limit_rph` | integer, nullable | Same semantics. |

**Effective limit** for a key = per-key value if not NULL, else system default. `0` is the sentinel for "explicitly unlimited" — never exposed as a number in the UI; the frontend uses an "Unlimited" checkbox.

### KeyScope extension

Add `rate_limit_rpm: int | None` and `rate_limit_rph: int | None` to `KeyScope` dataclass. Populated in `_resolve_principal()` (MCP auth) and available on `principal.key_scope` at call time — no per-request DB lookups needed.

## Enforcement

### In-memory sliding window

A `RateLimiter` singleton holds per-key sliding windows. Each window is a deque of request timestamps.

```
RateLimiter:
  check(key_id, rpm_limit, rph_limit) -> (allowed: bool, retry_after_seconds: int)
```

`check()` counts entries in the last 60s and 3600s, compares against limits, prunes entries older than 1 hour. Returns `retry_after_seconds` = seconds until the oldest entry in the violated window expires.

**Concurrency:** The `RateLimiter` is accessed from async coroutines on a single event loop (no threads). Deque operations are atomic in CPython, but the "check count + append" sequence is not. Use a per-key `asyncio.Lock` (stored in a `dict[UUID, asyncio.Lock]`, lazily created) to make check-and-record atomic. This also guards crash recovery seeding.

### Crash recovery

On the first request from a key after process startup, the limiter seeds the window from `api_request_log` — one query for the last hour of timestamps for that key. The per-key `asyncio.Lock` ensures only one coroutine seeds; concurrent requests for the same cold key wait on the lock rather than double-seeding. Cached thereafter. If `api_request_log` is unavailable, the window starts empty (fail-open).

### Enforcement points

Two enforcement points, each resolving effective limits from their available context:

1. **MCP tool calls** — at the top of `ScopedFastMCP.call_tool()`, before the existing denial check. Reads limits from `principal.key_scope.rate_limit_rpm/rph` (populated at auth time from the `ApiKey` model). If rate-limited, raise `ToolError("Rate limit exceeded. Try again in {N} seconds.")`.

2. **REST API requests** — in `ApiKeyRequestLogMiddleware`. The middleware already queries the `ApiKey` by token hash; expand the existing `select(ApiKey.key_id)` to also fetch `rate_limit_rpm` and `rate_limit_rph`. If rate-limited, short-circuit with HTTP 429 + `Retry-After: {N}` header + JSON body `{"detail": "Rate limit exceeded", "retry_after": N}`.

### Effective limit resolution

Both paths resolve effective limits the same way:

```python
rpm = key_rpm if key_rpm is not None else settings.default_rate_limit_rpm
rph = key_rph if key_rph is not None else settings.default_rate_limit_rph
# 0 means unlimited — skip that window
```

### Logging

Rate-limited requests are logged to `api_request_log` with `status="rate_limited"` (new value alongside `ok`, `error`, `denied`). Log level: **WARN**.

**Existing route updates required:** The usage summary endpoint's COUNT FILTER queries, the timeline pivot, and the dashboard all hard-code `ok`/`error`/`denied`. All three must add `rate_limited` as a fourth status — otherwise rate-limited events are silently dropped from aggregations.

## Frontend

### System Settings

New "Rate Limits" section (under the existing Settings hub):

- **Default requests/minute** — number input, min 1
- **Default requests/hour** — number input, min 1

### API Key edit modal

Two new field groups in the scope form:

- **Requests/minute** — "Unlimited" checkbox (checked = store 0). Unchecked + empty = NULL (use system default). Unchecked + value = custom limit (validated >=1). Helper text: "System default: {N}/min"
- **Requests/hour** — same pattern.

UI rejects values < 1 (frontend validation). Backend also validates: reject < 1 unless exactly 0 (unlimited).

The `_scope_summary` includes rate limits when set: e.g., "Read only, 3 topics, 30 rpm, 1000 rph".

### API Key create form

Same fields as the edit modal. New keys default to NULL (system default applies).

### Audit dashboard

- `status="rate_limited"` rows get an orange badge in the request log table (distinct from amber "denied")
- New **Rate limited** summary card with dropdown (1h / 24h / 7d), same pattern as Errors/Denials
- Timeline chart: 4th stacked bar color for rate-limited (orange)

## API Changes

### PATCH /api/api-keys/{key_id}

Accepts `rate_limit_rpm` and `rate_limit_rph` in the patch body (same partial-update semantics as existing fields). Validates: null (clear override), 0 (unlimited), or >=1.

### POST /api/api-keys

Accepts `rate_limit_rpm` and `rate_limit_rph` in the create body. Same validation.

### GET /api/api-keys

Response includes `rate_limit_rpm` and `rate_limit_rph` per key.

### GET /api/api-keys/{key_id}/usage

Add `rate_limited` to the error/denial bucket pattern:
```json
{
  "rate_limited": {"1h": 0, "24h": 2, "7d": 5},
  ...
}
```

### GET/PATCH /api/system/settings

Expose `default_rate_limit_rpm` and `default_rate_limit_rph`. Admin-only.

## Files

| File | Change |
|---|---|
| `alembic/versions/0015_rate_limits.py` | Add `rate_limit_rpm`, `rate_limit_rph` to `api_keys` |
| `src/harbor_clerk/models/api_key.py` | Two new columns |
| `src/harbor_clerk/models/model_settings.py` | Two new default fields (or new row) |
| `src/harbor_clerk/api/scope.py` | Add `rate_limit_rpm`, `rate_limit_rph` to `KeyScope` |
| `src/harbor_clerk/api/deps.py` | Populate rate limit fields on `KeyScope` in `get_current_principal()` |
| `src/harbor_clerk/mcp_server.py` | Populate rate limit fields in `_resolve_principal()` |
| `src/harbor_clerk/api/rate_limiter.py` | New: `RateLimiter` class with sliding window, per-key lock, crash recovery |
| `src/harbor_clerk/mcp_server.py` | Rate limit check in `ScopedFastMCP.call_tool()` |
| `src/harbor_clerk/api/middleware.py` | Expand ApiKey query + rate limit check |
| `src/harbor_clerk/api/routes/api_keys.py` | Accept/return rate limit fields; add `rate_limited` to usage buckets |
| `src/harbor_clerk/api/schemas/api_keys.py` | Add fields to request/response schemas |
| `src/harbor_clerk/api/routes/system.py` | Expose default rate limit settings |
| `frontend/src/pages/ApiKeysPage.tsx` | Rate limit fields with unlimited checkbox |
| `frontend/src/pages/ApiKeyDashboardPage.tsx` | Rate limited card + timeline bar + orange badge |
| `frontend/src/pages/SystemSettingsPage.tsx` | Default rate limit inputs |
| `tests/test_rate_limiter.py` | Unit tests: sliding window, crash recovery, concurrency, effective limit resolution |
