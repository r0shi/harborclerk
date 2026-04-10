# Per-API-Key Rate Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-minute and per-hour rate limiting for API key requests, with system-wide defaults, in-memory enforcement, and audit dashboard integration.

**Architecture:** Two new columns on `api_keys` (migration 0015). `KeyScope` extended to carry rate limit values at auth time. New `RateLimiter` singleton with sliding window + per-key asyncio.Lock + crash recovery from `api_request_log`. Enforcement in `ScopedFastMCP.call_tool()` and `ApiKeyRequestLogMiddleware`. System defaults via Settings + sync_native_config pattern.

**Tech Stack:** PostgreSQL, SQLAlchemy 2.0 async, Alembic, FastAPI, asyncio, React 19, Tailwind CSS 4

**Spec:** `docs/superpowers/specs/2026-04-10-rate-limits-design.md`

---

### Task 1: Migration + Model

**Files:**
- Create: `alembic/versions/0015_rate_limits.py`
- Modify: `src/harbor_clerk/models/api_key.py`

- [ ] **Step 1: Add columns to ApiKey model**

In `src/harbor_clerk/models/api_key.py`, add after `max_snippet_chars`:

```python
rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
rate_limit_rph: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 2: Write migration 0015**

Create `alembic/versions/0015_rate_limits.py`:

```python
"""Add rate_limit_rpm and rate_limit_rph to api_keys.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("rate_limit_rpm", sa.Integer(), nullable=True))
    op.add_column("api_keys", sa.Column("rate_limit_rph", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "rate_limit_rph")
    op.drop_column("api_keys", "rate_limit_rpm")
```

- [ ] **Step 3: Run migration and verify**

```bash
cd /Users/alex/mcp-gateway
alembic upgrade head
uv run ruff check src/harbor_clerk/models/api_key.py alembic/versions/0015_rate_limits.py
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/models/api_key.py alembic/versions/0015_rate_limits.py
git commit -m "feat: add rate_limit_rpm/rph columns to api_keys (migration 0015)"
```

---

### Task 2: KeyScope + Principal Extension

**Files:**
- Modify: `src/harbor_clerk/api/scope.py`
- Modify: `src/harbor_clerk/mcp_server.py` (lines ~103-110, _resolve_principal)
- Modify: `src/harbor_clerk/api/deps.py` (lines ~90-96, get_current_principal)
- Test: `tests/test_scoped_api_keys.py` (extend)

- [ ] **Step 1: Add rate limit fields to KeyScope**

In `src/harbor_clerk/api/scope.py`, add two fields to the `KeyScope` dataclass:

```python
rate_limit_rpm: int | None
rate_limit_rph: int | None
```

- [ ] **Step 2: Populate in _resolve_principal (MCP path)**

In `src/harbor_clerk/mcp_server.py`, in `_resolve_principal()`, where `KeyScope` is constructed (around line 103-110), add the two new fields:

```python
scope = KeyScope(
    scope_topic_ids=api_key.scope_topic_ids,
    scope_folder_ids=api_key.scope_folder_ids,
    permission_tier=api_key.permission_tier,
    tool_overrides=api_key.tool_overrides or {},
    max_snippet_chars=api_key.max_snippet_chars,
    rate_limit_rpm=api_key.rate_limit_rpm,
    rate_limit_rph=api_key.rate_limit_rph,
)
```

- [ ] **Step 3: Populate in get_current_principal (REST path)**

In `src/harbor_clerk/api/deps.py`, same change where `KeyScope` is constructed (around line 90-96):

```python
scope = KeyScope(
    scope_topic_ids=api_key.scope_topic_ids,
    scope_folder_ids=api_key.scope_folder_ids,
    permission_tier=api_key.permission_tier,
    tool_overrides=api_key.tool_overrides or {},
    max_snippet_chars=api_key.max_snippet_chars,
    rate_limit_rpm=api_key.rate_limit_rpm,
    rate_limit_rph=api_key.rate_limit_rph,
)
```

- [ ] **Step 4: Fix existing tests**

Any existing test that constructs `KeyScope` directly (e.g., in `tests/test_scoped_api_keys.py`, `tests/test_mcp_tool_filtering.py`) will fail because of the two new required fields. Add `rate_limit_rpm=None, rate_limit_rph=None` to all existing `KeyScope(...)` calls.

Also update `src/harbor_clerk/api/routes/api_keys.py` where `KeyScope` is constructed in `scope_preview` and `scope_preview_adhoc` endpoints.

- [ ] **Step 5: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/api/scope.py src/harbor_clerk/api/deps.py src/harbor_clerk/mcp_server.py
uv run pytest tests/test_scoped_api_keys.py tests/test_mcp_tool_filtering.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/scope.py src/harbor_clerk/api/deps.py src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/api_keys.py tests/
git commit -m "feat: extend KeyScope with rate_limit_rpm/rph fields"
```

---

### Task 3: RateLimiter Class

**Files:**
- Create: `src/harbor_clerk/api/rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

- [ ] **Step 1: Write the RateLimiter**

Create `src/harbor_clerk/api/rate_limiter.py`:

```python
"""In-memory sliding window rate limiter with crash recovery."""

import asyncio
import logging
import time
import uuid
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """Per-key sliding window rate limiter.

    Maintains a deque of request timestamps per key. check() counts
    entries in the last 60s/3600s and compares against limits.

    Thread-safe via per-key asyncio.Lock (prevents double-seeding
    and ensures check-and-record is atomic).
    """

    def __init__(self):
        self._windows: dict[uuid.UUID, deque[float]] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._seeded: set[uuid.UUID] = set()

    def _get_lock(self, key_id: uuid.UUID) -> asyncio.Lock:
        if key_id not in self._locks:
            self._locks[key_id] = asyncio.Lock()
        return self._locks[key_id]

    def _get_window(self, key_id: uuid.UUID) -> deque[float]:
        if key_id not in self._windows:
            self._windows[key_id] = deque()
        return self._windows[key_id]

    async def _seed_if_needed(self, key_id: uuid.UUID) -> None:
        """Seed the window from api_request_log on first access after startup."""
        if key_id in self._seeded:
            return
        try:
            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models.api_request_log import ApiRequestLog
            from sqlalchemy import select

            async with async_session_factory() as session:
                cutoff = time.time() - 3600
                from datetime import UTC, datetime, timedelta

                rows = (
                    await session.execute(
                        select(ApiRequestLog.created_at)
                        .where(
                            ApiRequestLog.api_key_id == key_id,
                            ApiRequestLog.created_at >= datetime.now(UTC) - timedelta(hours=1),
                            ApiRequestLog.status.in_(["ok", "error"]),
                        )
                        .order_by(ApiRequestLog.created_at)
                    )
                ).scalars().all()

                window = self._get_window(key_id)
                for ts in rows:
                    window.append(ts.timestamp())
        except Exception:
            logger.debug("Failed to seed rate limiter for key %s", key_id, exc_info=True)
        self._seeded.add(key_id)

    def _prune(self, window: deque[float], now: float) -> None:
        """Remove entries older than 1 hour."""
        cutoff = now - 3600
        while window and window[0] < cutoff:
            window.popleft()

    async def check(
        self,
        key_id: uuid.UUID,
        rpm_limit: int,
        rph_limit: int,
    ) -> tuple[bool, int]:
        """Check rate limit and record the request if allowed.

        Returns (allowed, retry_after_seconds).
        If allowed is True, the request timestamp is recorded.
        If allowed is False, retry_after_seconds indicates when to retry.
        """
        if rpm_limit == 0 and rph_limit == 0:
            return True, 0  # both unlimited

        lock = self._get_lock(key_id)
        async with lock:
            await self._seed_if_needed(key_id)
            window = self._get_window(key_id)
            now = time.time()
            self._prune(window, now)

            # Check per-minute limit
            if rpm_limit > 0:
                minute_cutoff = now - 60
                minute_count = sum(1 for t in window if t >= minute_cutoff)
                if minute_count >= rpm_limit:
                    oldest_in_window = next(t for t in window if t >= minute_cutoff)
                    retry_after = int(oldest_in_window + 60 - now) + 1
                    return False, max(retry_after, 1)

            # Check per-hour limit
            if rph_limit > 0:
                hour_count = len(window)  # already pruned to 1h
                if hour_count >= rph_limit:
                    retry_after = int(window[0] + 3600 - now) + 1
                    return False, max(retry_after, 1)

            # Allowed — record timestamp
            window.append(now)
            return True, 0


# Module-level singleton
rate_limiter = RateLimiter()
```

- [ ] **Step 2: Write tests**

Create `tests/test_rate_limiter.py`:

```python
"""Tests for the in-memory sliding window rate limiter."""

import asyncio
import time
import uuid

import pytest

from harbor_clerk.api.rate_limiter import RateLimiter


@pytest.mark.anyio
async def test_allows_under_limit():
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)  # skip DB seed

    allowed, retry = await limiter.check(key, rpm_limit=10, rph_limit=100)
    assert allowed is True
    assert retry == 0


@pytest.mark.anyio
async def test_blocks_over_rpm():
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)

    for _ in range(5):
        allowed, _ = await limiter.check(key, rpm_limit=5, rph_limit=1000)
        assert allowed is True

    allowed, retry = await limiter.check(key, rpm_limit=5, rph_limit=1000)
    assert allowed is False
    assert retry >= 1


@pytest.mark.anyio
async def test_blocks_over_rph():
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)

    for _ in range(3):
        allowed, _ = await limiter.check(key, rpm_limit=100, rph_limit=3)
        assert allowed is True

    allowed, retry = await limiter.check(key, rpm_limit=100, rph_limit=3)
    assert allowed is False
    assert retry >= 1


@pytest.mark.anyio
async def test_unlimited_always_allows():
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)

    for _ in range(100):
        allowed, _ = await limiter.check(key, rpm_limit=0, rph_limit=0)
        assert allowed is True


@pytest.mark.anyio
async def test_independent_keys():
    limiter = RateLimiter()
    key_a = uuid.uuid4()
    key_b = uuid.uuid4()
    limiter._seeded.update({key_a, key_b})

    for _ in range(5):
        await limiter.check(key_a, rpm_limit=5, rph_limit=1000)

    # key_a is at limit
    allowed_a, _ = await limiter.check(key_a, rpm_limit=5, rph_limit=1000)
    assert allowed_a is False

    # key_b is still fine
    allowed_b, _ = await limiter.check(key_b, rpm_limit=5, rph_limit=1000)
    assert allowed_b is True


@pytest.mark.anyio
async def test_prune_old_entries():
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)

    # Manually insert old timestamps
    window = limiter._get_window(key)
    old_time = time.time() - 3700  # older than 1 hour
    for _ in range(10):
        window.append(old_time)

    # Should be pruned and allow new requests
    allowed, _ = await limiter.check(key, rpm_limit=5, rph_limit=5)
    assert allowed is True
    assert len(window) == 1  # only the new one


@pytest.mark.anyio
async def test_concurrent_checks_atomic():
    """Two concurrent checks shouldn't both succeed when only one slot remains."""
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)

    # Fill to limit - 1
    for _ in range(4):
        await limiter.check(key, rpm_limit=5, rph_limit=1000)

    # Two concurrent checks for the last slot
    results = await asyncio.gather(
        limiter.check(key, rpm_limit=5, rph_limit=1000),
        limiter.check(key, rpm_limit=5, rph_limit=1000),
    )
    allowed_count = sum(1 for allowed, _ in results if allowed)
    assert allowed_count == 1  # exactly one should succeed
```

- [ ] **Step 3: Run tests**

```bash
uv run ruff check src/harbor_clerk/api/rate_limiter.py tests/test_rate_limiter.py
uv run pytest tests/test_rate_limiter.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/api/rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: in-memory sliding window rate limiter with crash recovery"
```

---

### Task 4: MCP Enforcement

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`

- [ ] **Step 1: Add rate limit check in call_tool**

In `ScopedFastMCP.call_tool()`, after the `if principal.type != "api_key"` early return and before the denial check, add:

```python
# --- Rate limit check ---
if principal.key_scope is not None:
    from harbor_clerk.api.rate_limiter import rate_limiter
    from harbor_clerk.config import get_settings

    rl_settings = get_settings()
    rpm = (
        principal.key_scope.rate_limit_rpm
        if principal.key_scope.rate_limit_rpm is not None
        else rl_settings.default_rate_limit_rpm
    )
    rph = (
        principal.key_scope.rate_limit_rph
        if principal.key_scope.rate_limit_rph is not None
        else rl_settings.default_rate_limit_rph
    )
    allowed, retry_after = await rate_limiter.check(principal.id, rpm, rph)
    if not allowed:
        logger.warning(
            "Rate limit exceeded for key %s: %d rpm / %d rph (retry_after=%ds)",
            principal.id, rpm, rph, retry_after,
        )
        try:
            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="mcp_tool",
                    endpoint=name,
                    parameters=dict(arguments) if arguments else None,
                    status="rate_limited",
                    status_detail=f"retry_after={retry_after}s",
                    duration_ms=0,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log rate-limited MCP call", exc_info=True)

        from mcp.server.fastmcp.exceptions import ToolError
        raise ToolError(f"Rate limit exceeded. Try again in {retry_after} seconds.")
```

- [ ] **Step 2: Add default settings to config.py**

In `src/harbor_clerk/config.py`, add to the Settings class:

```python
# Rate limiting
default_rate_limit_rpm: int = Field(default=60)
default_rate_limit_rph: int = Field(default=2000)
```

- [ ] **Step 3: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py src/harbor_clerk/config.py
uv run pytest tests/test_mcp_tool_filtering.py tests/test_scoped_api_keys.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/mcp_server.py src/harbor_clerk/config.py
git commit -m "feat: rate limit enforcement in MCP call_tool"
```

---

### Task 5: REST Middleware Enforcement

**Files:**
- Modify: `src/harbor_clerk/api/middleware.py`

- [ ] **Step 1: Expand ApiKey query and add rate limit check**

In `ApiKeyRequestLogMiddleware.__call__()`, expand the existing `select(ApiKey.key_id)` to also fetch rate limit columns. Then add a rate limit check before forwarding to the inner app.

The current flow is: detect API key → forward to app → log after response. Change to: detect API key → check rate limit → if limited, return 429 + log → else forward to app → log after response.

The key change: the rate limit check must happen BEFORE `await self.app(scope, receive, send_wrapper)`, not after. Restructure the middleware to query ApiKey fields first, check rate limit, then either reject or proceed.

```python
# After token extraction and before forwarding:
key_hash = hash_api_key(token)
async with async_session_factory() as check_session:
    row = (
        await check_session.execute(
            select(ApiKey.key_id, ApiKey.rate_limit_rpm, ApiKey.rate_limit_rph)
            .where(ApiKey.key_hash == key_hash)
        )
    ).one_or_none()
    if row is None:
        await self.app(scope, receive, send)
        return

    key_id, key_rpm, key_rph = row

# Resolve effective limits
settings = get_settings()
rpm = key_rpm if key_rpm is not None else settings.default_rate_limit_rpm
rph = key_rph if key_rph is not None else settings.default_rate_limit_rph

allowed, retry_after = await rate_limiter.check(key_id, rpm, rph)
if not allowed:
    # Return 429 directly
    logger.warning("Rate limit exceeded for key %s on REST %s %s", key_id, method, path)
    # ... send 429 response with Retry-After header ...
    # ... log to api_request_log with status="rate_limited" ...
    return

# Proceed with request
await self.app(scope, receive, send_wrapper)
# ... log success/error as before ...
```

The implementer should read the full middleware and restructure it. The rate limit check goes between the key lookup and the `self.app()` call. The 429 response uses the same ASGI send pattern as `MCPTokenPathAuth._send_401()`.

- [ ] **Step 2: Run lint**

```bash
uv run ruff check src/harbor_clerk/api/middleware.py
```

- [ ] **Step 3: Commit**

```bash
git add src/harbor_clerk/api/middleware.py
git commit -m "feat: rate limit enforcement in REST middleware"
```

---

### Task 6: API Schemas + Endpoints

**Files:**
- Modify: `src/harbor_clerk/api/schemas/api_keys.py`
- Modify: `src/harbor_clerk/api/routes/api_keys.py`

- [ ] **Step 1: Add rate limit fields to schemas**

In `src/harbor_clerk/api/schemas/api_keys.py`:

Add to `CreateApiKeyRequest`:
```python
rate_limit_rpm: int | None = None
rate_limit_rph: int | None = None
```

Add to `PatchApiKeyRequest`:
```python
rate_limit_rpm: int | None = None
rate_limit_rph: int | None = None
```

Add to `ApiKeyOut`:
```python
rate_limit_rpm: int | None
rate_limit_rph: int | None
```

- [ ] **Step 2: Update route handlers**

In `src/harbor_clerk/api/routes/api_keys.py`:

- `create_api_key`: pass `rate_limit_rpm` and `rate_limit_rph` to the ApiKey constructor
- `_api_key_to_out`: include the two new fields
- `_scope_summary`: append rate limit info when set (e.g., "60 rpm", "2000 rph")
- Validation in PATCH: reject `rate_limit_rpm < 1` unless exactly 0 or None

- [ ] **Step 3: Add `rate_limited` to usage summary**

In `get_key_usage`, add `rate_limited` FILTER counts alongside the existing error/denial buckets:

```python
for label, hours in error_buckets.items():
    cutoff = now - timedelta(hours=hours)
    # ... existing err and den columns ...
    columns.append(
        func.count(ApiRequestLog.request_id).filter(ts >= cutoff, st == "rate_limited").label(f"rl_{label}")
    )

# In the response:
rate_limited = {label: getattr(row, f"rl_{label}") for label in error_buckets}
```

Also update the timeline endpoint to include `rate_limited` in the `by_day` pivot.

- [ ] **Step 4: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/api/routes/api_keys.py src/harbor_clerk/api/schemas/api_keys.py
uv run pytest tests/test_api_request_log.py tests/test_api_request_log_endpoints.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/api_keys.py src/harbor_clerk/api/schemas/api_keys.py
git commit -m "feat: rate limit fields in API key schemas + usage summary"
```

---

### Task 7: System Default Settings

**Files:**
- Modify: `src/harbor_clerk/api/routes/system.py`

- [ ] **Step 1: Add GET/PUT endpoints for rate limit defaults**

Follow the existing `retrieval-settings` pattern in `system.py`:

```python
@router.get("/system/rate-limit-settings")
async def get_rate_limit_settings(admin: Principal = Depends(require_admin)):
    s = get_settings()
    return {
        "default_rate_limit_rpm": s.default_rate_limit_rpm,
        "default_rate_limit_rph": s.default_rate_limit_rph,
    }

@router.put("/system/rate-limit-settings")
async def update_rate_limit_settings(
    body: RateLimitSettingsUpdate,
    admin: Principal = Depends(require_admin),
):
    s = get_settings()
    if body.default_rate_limit_rpm is not None:
        s.default_rate_limit_rpm = body.default_rate_limit_rpm
        sync_native_config("default_rate_limit_rpm", body.default_rate_limit_rpm)
    if body.default_rate_limit_rph is not None:
        s.default_rate_limit_rph = body.default_rate_limit_rph
        sync_native_config("default_rate_limit_rph", body.default_rate_limit_rph)
    return {
        "default_rate_limit_rpm": s.default_rate_limit_rpm,
        "default_rate_limit_rph": s.default_rate_limit_rph,
    }
```

Add a Pydantic model for the update body with `min=1` validation.

- [ ] **Step 2: Run lint**

```bash
uv run ruff check src/harbor_clerk/api/routes/system.py
```

- [ ] **Step 3: Commit**

```bash
git add src/harbor_clerk/api/routes/system.py
git commit -m "feat: system default rate limit settings endpoints"
```

---

### Task 8: Frontend — API Key Form Fields

**Files:**
- Modify: `frontend/src/pages/ApiKeysPage.tsx`

- [ ] **Step 1: Add rate limit fields to ScopeFormFields**

Add two field groups to `ScopeFormFields`, following the expiry checkbox pattern:

Each has an "Unlimited" checkbox. When unchecked, a number input appears (min 1). When checked, stores `0`. When neither checked nor filled, stores null (use system default).

The `ScopeFormState` interface gets:
```typescript
rateLimitRpm: string  // '' = system default, '0' = unlimited, else custom
rateLimitRpmUnlimited: boolean
rateLimitRph: string
rateLimitRphUnlimited: boolean
```

`scopeStateToPayload` maps: unlimited checkbox → 0, empty string → null, number → the number.

Helper text under each field: "System default: {N}/min" / "System default: {N}/hour" (fetch from a new settings endpoint or hardcode initially).

- [ ] **Step 2: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/ApiKeysPage.tsx
npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/ApiKeysPage.tsx
git commit -m "feat: rate limit fields with unlimited checkbox in API key form"
```

---

### Task 9: Frontend — Dashboard Integration

**Files:**
- Modify: `frontend/src/pages/ApiKeyDashboardPage.tsx`

- [ ] **Step 1: Add rate limited card and timeline color**

In `ApiKeyDashboardPage.tsx`:

1. Add `rate_limited` to the `UsageSummary` interface
2. Add a new summary card: "Rate Limited" with dropdown (1h / 24h / 7d), orange-tinted when > 0
3. Add a 4th stacked bar to the timeline chart: `rate_limited` in orange (`#f97316`)
4. Add an orange badge for `status="rate_limited"` in the `statusBadge` function
5. Add orange left border for rate-limited rows in the request log table

- [ ] **Step 2: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/ApiKeyDashboardPage.tsx
npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/ApiKeyDashboardPage.tsx
git commit -m "feat: rate limited card + timeline bar + badge in audit dashboard"
```

---

### Task 10: Frontend — System Settings

**Files:**
- Modify: `frontend/src/pages/SystemSettingsPage.tsx` (or the appropriate settings page)

- [ ] **Step 1: Add rate limit default inputs**

Add a "Rate Limits" section to the System Settings page with:
- Default requests/minute — number input, min 1
- Default requests/hour — number input, min 1
- Save button that PATCHes `/api/system/rate-limit-settings`

Follow the existing pattern for whatever settings are already on this page.

- [ ] **Step 2: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/SystemSettingsPage.tsx
npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/SystemSettingsPage.tsx
git commit -m "feat: system default rate limit settings in admin UI"
```

---

### Task 11: Final Verification

- [ ] **Step 1: Update _EXPECTED_SCHEMA_VERSION**

In `src/harbor_clerk/api/app.py`, update `_EXPECTED_SCHEMA_VERSION = "0015"`.

- [ ] **Step 2: Full lint pass**

```bash
cd /Users/alex/mcp-gateway
uv run ruff check src/harbor_clerk/
uv run ruff format --check src/harbor_clerk/
cd frontend && npx eslint src/ && npx tsc --noEmit
```

- [ ] **Step 3: Run all tests**

```bash
cd /Users/alex/mcp-gateway
uv run pytest tests/ -v
```

- [ ] **Step 4: Manual smoke test**

1. Create an API key with rate_limit_rpm=5
2. Make 5 MCP tool calls → all succeed
3. 6th call → 429 / ToolError with retry_after
4. Wait 60s, try again → succeeds
5. Check audit dashboard → rate_limited events visible
6. System Settings → change default RPM → new keys use it

- [ ] **Step 5: Commit any final fixes**

```bash
git add -A
git commit -m "fix: final adjustments for rate limiting"
```
