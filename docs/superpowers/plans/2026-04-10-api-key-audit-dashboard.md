# Per-API-Key Audit Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-request logging for API key usage (MCP + REST) and a dashboard page showing usage stats, tool breakdowns, and a filterable request log.

**Architecture:** New `api_request_log` table (migration 0014). Instrumentation in `ScopedFastMCP.call_tool()` and a new REST middleware. Four new API endpoints under `/api/api-keys/{key_id}/usage`. New React page at `/admin/api-keys/:keyId` with recharts visualizations.

**Tech Stack:** PostgreSQL, SQLAlchemy 2.0 async, Alembic, FastAPI, React 19, recharts, Tailwind CSS 4

**Spec:** `docs/superpowers/specs/2026-04-10-api-key-audit-dashboard-design.md`

---

### Task 1: Model + Migration

**Files:**
- Create: `src/harbor_clerk/models/api_request_log.py`
- Modify: `src/harbor_clerk/models/__init__.py`
- Create: `alembic/versions/0014_api_request_log.py`
- Test: `tests/test_api_request_log.py`

- [ ] **Step 1: Write the model**

Create `src/harbor_clerk/models/api_request_log.py`:

```python
"""API request log — per-request telemetry for API key usage."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class ApiRequestLog(Base):
    __tablename__ = "api_request_log"
    __table_args__ = (
        Index("ix_api_request_log_key_time_ep", "api_key_id", created_at.desc(), "endpoint"),
        Index("ix_api_request_log_created", "created_at"),
        Index("ix_api_request_log_endpoint", "endpoint"),
    )

    request_id: Mapped[uuid_pk]
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_keys.key_id", ondelete="SET NULL"),
        nullable=True,
    )
    request_type: Mapped[str] = mapped_column(Text, nullable=False)  # "mcp_tool" or "rest"
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # "ok", "error", "denied"
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[created_at]
```

Note: The `created_at` annotation in `__table_args__` Index references may need to use the column directly. The implementer should verify the index definition compiles — if the `created_at` mapped annotation doesn't work in `Index()`, use `sa.text("created_at DESC")` or define the index in the migration instead.

- [ ] **Step 2: Register in models/__init__.py**

Add to `src/harbor_clerk/models/__init__.py`:

```python
from harbor_clerk.models.api_request_log import ApiRequestLog
```

And add `"ApiRequestLog"` to the `__all__` list.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0014_api_request_log.py`:

```python
"""Add api_request_log table for per-request API key telemetry.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_request_log",
        sa.Column("request_id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("api_key_id", sa.UUID(), sa.ForeignKey("api_keys.key_id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_type", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_api_request_log_key_time_ep", "api_request_log", ["api_key_id", sa.text("created_at DESC"), "endpoint"])
    op.create_index("ix_api_request_log_created", "api_request_log", ["created_at"])
    op.create_index("ix_api_request_log_endpoint", "api_request_log", ["endpoint"])


def downgrade() -> None:
    op.drop_index("ix_api_request_log_endpoint")
    op.drop_index("ix_api_request_log_created")
    op.drop_index("ix_api_request_log_key_time_ep")
    op.drop_table("api_request_log")
```

- [ ] **Step 4: Write tests for the model**

Create `tests/test_api_request_log.py`:

```python
"""Tests for API request log model and helper."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.api_request_log import ApiRequestLog


@pytest.mark.anyio
async def test_create_request_log_entry(db_session: AsyncSession):
    """Basic insert and read-back."""
    entry = ApiRequestLog(
        api_key_id=None,
        request_type="mcp_tool",
        endpoint="kb_search",
        parameters={"query": "test", "k": 10},
        status="ok",
        result_summary={"count": 5},
        duration_ms=42,
    )
    db_session.add(entry)
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.request_id == entry.request_id))
    row = result.scalar_one()
    assert row.endpoint == "kb_search"
    assert row.status == "ok"
    assert row.parameters["query"] == "test"
    assert row.duration_ms == 42
```

- [ ] **Step 5: Run migration and tests**

```bash
cd /Users/alex/mcp-gateway
alembic upgrade head
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/models/api_request_log.py src/harbor_clerk/models/__init__.py alembic/versions/0014_api_request_log.py tests/test_api_request_log.py
git commit -m "feat: api_request_log model and migration 0014"
```

---

### Task 2: Logging Helper

**Files:**
- Create: `src/harbor_clerk/api/request_log.py`
- Test: `tests/test_api_request_log.py` (extend)

- [ ] **Step 1: Write the helper**

Create `src/harbor_clerk/api/request_log.py`:

```python
"""API request logging helper — writes per-request telemetry to api_request_log."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.api_request_log import ApiRequestLog


async def log_api_request(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    request_type: str,
    endpoint: str,
    parameters: dict[str, Any] | None = None,
    status: str = "ok",
    status_detail: str | None = None,
    result_summary: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> None:
    """Insert a single request log entry. Caller must commit."""
    entry = ApiRequestLog(
        api_key_id=api_key_id,
        request_type=request_type,
        endpoint=endpoint,
        parameters=parameters,
        status=status,
        status_detail=status_detail,
        result_summary=result_summary,
        duration_ms=duration_ms,
    )
    session.add(entry)
```

- [ ] **Step 2: Add test for the helper**

Append to `tests/test_api_request_log.py`:

```python
from harbor_clerk.api.request_log import log_api_request


@pytest.mark.anyio
async def test_log_api_request_helper(db_session: AsyncSession, admin_user):
    """log_api_request creates an entry with all fields."""
    key_id = uuid.uuid4()
    await log_api_request(
        db_session,
        api_key_id=key_id,
        request_type="mcp_tool",
        endpoint="kb_search",
        parameters={"query": "budget"},
        status="ok",
        result_summary={"count": 3},
        duration_ms=55,
    )
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.endpoint == "kb_search"))
    row = result.scalar_one()
    assert row.api_key_id == key_id
    assert row.duration_ms == 55


@pytest.mark.anyio
async def test_log_api_request_denied(db_session: AsyncSession):
    """Denied requests are logged with status_detail."""
    await log_api_request(
        db_session,
        api_key_id=uuid.uuid4(),
        request_type="mcp_tool",
        endpoint="kb_reprocess",
        status="denied",
        status_detail="tool not in search tier",
        duration_ms=0,
    )
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.status == "denied"))
    row = result.scalar_one()
    assert row.status_detail == "tool not in search tier"
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/api/request_log.py tests/test_api_request_log.py
git commit -m "feat: log_api_request helper for request telemetry"
```

---

### Task 3: MCP Tool Call Instrumentation

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — `ScopedFastMCP.call_tool()` method (~line 290)
- Test: `tests/test_api_request_log.py` (extend)

- [ ] **Step 1: Instrument `call_tool`**

Modify `ScopedFastMCP.call_tool()` in `src/harbor_clerk/mcp_server.py`. The current method (around line 290):

```python
async def call_tool(self, name, arguments):
    try:
        principal = _get_principal()
    except PermissionError:
        return await super().call_tool(name, arguments)
    if (
        principal.type == "api_key"
        and principal.key_scope is not None
        and name not in principal.key_scope.effective_tools
    ):
        from mcp.server.fastmcp.exceptions import ToolError
        raise ToolError(f"Unknown tool: {name}")
    return await super().call_tool(name, arguments)
```

Replace with:

```python
async def call_tool(self, name, arguments):
    try:
        principal = _get_principal()
    except PermissionError:
        return await super().call_tool(name, arguments)

    # Only log for API key principals
    if principal.type != "api_key":
        return await super().call_tool(name, arguments)

    import time
    from harbor_clerk.api.request_log import log_api_request
    from harbor_clerk.db import async_session_factory

    # Check tool access before executing
    if principal.key_scope is not None and name not in principal.key_scope.effective_tools:
        # Log the denial
        try:
            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="mcp_tool",
                    endpoint=name,
                    parameters=dict(arguments) if arguments else None,
                    status="denied",
                    status_detail=f"tool not in {principal.key_scope.permission_tier} tier",
                    duration_ms=0,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log denied tool call", exc_info=True)

        from mcp.server.fastmcp.exceptions import ToolError
        raise ToolError(f"Unknown tool: {name}")

    # Execute the tool and log the result
    start = time.monotonic()
    status = "ok"
    status_detail = None
    result_summary = None
    try:
        result = await super().call_tool(name, arguments)
        # Try to extract result summary from the tool's JSON response
        try:
            for item in result.content:
                if hasattr(item, "text"):
                    parsed = json.loads(item.text)
                    if isinstance(parsed, dict):
                        summary = {}
                        for key in ("total_candidates", "count", "doc_count", "has_more"):
                            if key in parsed:
                                summary[key] = parsed[key]
                        if "hits" in parsed:
                            summary["count"] = len(parsed["hits"])
                        if "would_match_unscoped" in parsed:
                            summary["would_match_unscoped"] = parsed["would_match_unscoped"]
                        if summary:
                            result_summary = summary
                    break
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return result
    except Exception as exc:
        status = "error"
        status_detail = str(exc)[:500]
        raise
    finally:
        elapsed = int((time.monotonic() - start) * 1000)
        try:
            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="mcp_tool",
                    endpoint=name,
                    parameters=dict(arguments) if arguments else None,
                    status=status,
                    status_detail=status_detail,
                    result_summary=result_summary,
                    duration_ms=elapsed,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log tool call", exc_info=True)
```

- [ ] **Step 2: Add `would_match_unscoped` to `kb_search`**

In `src/harbor_clerk/mcp_server.py`, find the `kb_search` function. Locate the early return for empty `visible_ids` (around line 512):

```python
if visible_ids is not None:
    if not visible_ids:
        return json.dumps({"hits": [], "total_candidates": 0, "has_more": False}, indent=2)
```

Before the early return, add an unscoped count query:

```python
if visible_ids is not None:
    if not visible_ids:
        # Check if unscoped search would have results
        unscoped_count = 0
        try:
            unscoped_result = await hybrid_search(session, query, k=1)
            unscoped_count = unscoped_result.total_candidates
        except Exception:
            pass
        resp = {"hits": [], "total_candidates": 0, "has_more": False}
        if unscoped_count > 0:
            resp["would_match_unscoped"] = unscoped_count
        return json.dumps(resp, indent=2)
```

Also add the same check at the end of `kb_search`, after computing the final response but before `return json.dumps(resp, indent=2)`. If `visible_ids is not None` and `resp` has 0 hits but `total_candidates` from the scoped search was 0:

```python
# Annotate scope-filtered empty results
if visible_ids is not None and not resp.get("hits") and resp.get("total_candidates", 0) == 0:
    try:
        unscoped_result = await hybrid_search(session, query, k=1)
        if unscoped_result.total_candidates > 0:
            resp["would_match_unscoped"] = unscoped_result.total_candidates
    except Exception:
        pass
```

Note: This requires the `visible_ids` variable and `session` to still be in scope. The implementer should verify the exact placement. The hybrid_search call uses `k=1` to minimize overhead — we only need the count.

- [ ] **Step 3: Write tests**

Add to `tests/test_api_request_log.py`:

```python
@pytest.mark.anyio
async def test_mcp_tool_call_logged(client, admin_user, admin_token, db_session):
    """MCP tool calls via API key are logged to api_request_log."""
    # This test requires creating an API key and making an MCP tool call.
    # The exact mechanism depends on how the test client is wired for MCP.
    # Verify that after a kb_search call with an API key, a row exists in api_request_log.
    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.request_type == "mcp_tool"))
    # At minimum, verify the table is queryable and the instrumentation doesn't crash.
    rows = result.scalars().all()
    assert isinstance(rows, list)
```

Note: Full integration testing of MCP tool logging requires an API key + MCP transport setup. The implementer should check `tests/test_mcp_tool_filtering.py` for existing MCP test patterns and adapt.

- [ ] **Step 4: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py
uv run ruff format --check src/harbor_clerk/mcp_server.py
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_api_request_log.py
git commit -m "feat: instrument MCP tool calls with request logging"
```

---

### Task 4: REST Middleware Instrumentation

**Files:**
- Create: `src/harbor_clerk/api/middleware.py`
- Modify: `src/harbor_clerk/api/app.py` — register middleware
- Test: `tests/test_api_request_log.py` (extend)

- [ ] **Step 1: Write the middleware**

Create `src/harbor_clerk/api/middleware.py`:

```python
"""REST API request logging middleware for API key requests."""

import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Replace UUID-shaped path segments with {id}
_UUID_RE = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _normalize_path(method: str, path: str) -> str:
    """Normalize a request path: 'GET /api/docs/550e8400-...' → 'GET /api/docs/{id}'."""
    normalized = _UUID_RE.sub("/{id}", path)
    return f"{method} {normalized}"


class ApiKeyRequestLogMiddleware(BaseHTTPMiddleware):
    """Log REST API requests made with API keys.

    Skips human JWT users — only fires for api_key principals.
    Runs after authentication so the principal is available on request.state.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only log for API key principals
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.type != "api_key":
            return response

        # Skip non-API paths (static files, etc.)
        if not request.url.path.startswith("/api/"):
            return response

        elapsed = int((time.monotonic() - request.state.request_start) * 1000) if hasattr(request.state, "request_start") else 0
        endpoint = _normalize_path(request.method, request.url.path)
        status = "ok" if response.status_code < 400 else "error"
        status_detail = None if status == "ok" else f"HTTP {response.status_code}"

        try:
            from harbor_clerk.api.request_log import log_api_request
            from harbor_clerk.db import async_session_factory

            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="rest",
                    endpoint=endpoint,
                    parameters=dict(request.query_params) if request.query_params else None,
                    status=status,
                    status_detail=status_detail,
                    duration_ms=elapsed,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log REST request", exc_info=True)

        return response
```

Note: This middleware needs `request.state.principal` and `request.state.request_start` to be set. The implementer must check how the existing auth dependency (`get_current_principal` in `deps.py`) works. If it doesn't set `request.state`, the middleware needs to read the principal from the dependency injection context or use a separate approach. An alternative is a lightweight timing middleware that sets `request_start`, combined with setting `principal` on `request.state` in `get_current_principal`.

- [ ] **Step 2: Register middleware in app.py**

In `src/harbor_clerk/api/app.py`, in the `create_app()` function, after the existing `@app.middleware("http")` block:

```python
from harbor_clerk.api.middleware import ApiKeyRequestLogMiddleware
app.add_middleware(ApiKeyRequestLogMiddleware)
```

Also add a timing middleware that sets `request.state.request_start`:

```python
@app.middleware("http")
async def set_request_timing(request: Request, call_next):
    import time
    request.state.request_start = time.monotonic()
    return await call_next(request)
```

The implementer must also ensure `request.state.principal` is set. Modify `get_current_principal()` in `src/harbor_clerk/api/deps.py` to set `request.state.principal = principal` before returning. This requires adding `request: Request` as a dependency parameter.

- [ ] **Step 3: Write test for path normalization**

Add to `tests/test_api_request_log.py`:

```python
from harbor_clerk.api.middleware import _normalize_path


def test_normalize_path_with_uuid():
    assert _normalize_path("GET", "/api/docs/550e8400-e29b-41d4-a716-446655440000") == "GET /api/docs/{id}"


def test_normalize_path_without_uuid():
    assert _normalize_path("GET", "/api/api-keys") == "GET /api/api-keys"


def test_normalize_path_multiple_uuids():
    assert _normalize_path("GET", "/api/docs/550e8400-e29b-41d4-a716-446655440000/versions/660e8400-e29b-41d4-a716-446655440001") == "GET /api/docs/{id}/versions/{id}"
```

- [ ] **Step 4: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/api/middleware.py src/harbor_clerk/api/app.py src/harbor_clerk/api/deps.py
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/middleware.py src/harbor_clerk/api/app.py src/harbor_clerk/api/deps.py tests/test_api_request_log.py
git commit -m "feat: REST request logging middleware for API key requests"
```

---

### Task 5: Reaper (90-day purge)

**Files:**
- Modify: `src/harbor_clerk/api/app.py` — `_session_reaper_loop()`

- [ ] **Step 1: Add purge to reaper loop**

In `src/harbor_clerk/api/app.py`, inside `_session_reaper_loop()`, after the watched files cleanup block (around line 148) and before the `check_and_recompute_topics` call, add:

```python
# Purge API request log entries older than 90 days
try:
    from harbor_clerk.models.api_request_log import ApiRequestLog

    purge_cutoff = now - timedelta(days=90)
    purge_result = await db.execute(
        sa.delete(ApiRequestLog).where(ApiRequestLog.created_at < purge_cutoff)
    )
    purge_count = purge_result.rowcount
    if purge_count:
        logger.info("Reaper: purged %d expired API request log entries", purge_count)
    await db.commit()
except Exception:
    logger.warning("Reaper: failed to purge old API request logs", exc_info=True)
    await db.rollback()
```

Note: Use `sa.delete()` bulk delete instead of loading all rows into memory — the existing pattern loads rows but that's bad for potentially millions of log entries. `sa.delete().where(...)` runs a single SQL `DELETE` statement.

- [ ] **Step 2: Lint**

```bash
uv run ruff check src/harbor_clerk/api/app.py
```

- [ ] **Step 3: Commit**

```bash
git add src/harbor_clerk/api/app.py
git commit -m "feat: 90-day auto-purge for api_request_log in reaper"
```

---

### Task 6: API Endpoints — Usage Summary + Timeline

**Files:**
- Modify: `src/harbor_clerk/api/routes/api_keys.py`
- Modify: `src/harbor_clerk/api/schemas/api_keys.py`
- Test: `tests/test_api_request_log.py` (extend)

- [ ] **Step 1: Add response schemas**

Add to `src/harbor_clerk/api/schemas/api_keys.py`:

```python
class ToolCount(BaseModel):
    endpoint: str
    count: int


class UsageBuckets(BaseModel):
    h1: int = Field(alias="1h")
    h6: int = Field(alias="6h")
    h12: int = Field(alias="12h")
    h24: int = Field(alias="24h")
    d7: int = Field(alias="7d")
    d30: int = Field(alias="30d")

    model_config = {"populate_by_name": True}


class ErrorBuckets(BaseModel):
    h1: int = Field(alias="1h")
    h24: int = Field(alias="24h")
    d7: int = Field(alias="7d")

    model_config = {"populate_by_name": True}


class UsageSummaryResponse(BaseModel):
    requests: dict[str, int]
    errors: dict[str, int]
    denials: dict[str, int]
    last_used_at: datetime | None
    top_tools: dict[str, list[ToolCount]]


class TimelineDay(BaseModel):
    date: str
    ok: int
    error: int
    denied: int


class RequestLogEntry(BaseModel):
    request_id: str
    request_type: str
    endpoint: str
    parameters: dict | None
    status: str
    status_detail: str | None
    result_summary: dict | None
    duration_ms: int
    created_at: datetime


class RequestLogPage(BaseModel):
    items: list[RequestLogEntry]
    total: int
    page: int
    page_size: int


class PurgeResponse(BaseModel):
    deleted: int
```

- [ ] **Step 2: Add usage summary endpoint**

Add to `src/harbor_clerk/api/routes/api_keys.py`:

```python
from datetime import UTC, datetime, timedelta

from harbor_clerk.models.api_request_log import ApiRequestLog


@router.get("/api-keys/{key_id}/usage")
async def get_key_usage(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Summary stats for the per-key audit dashboard."""
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    now = datetime.now(UTC)

    # Request counts by time bucket
    request_buckets = {"1h": 1, "6h": 6, "12h": 12, "24h": 24, "7d": 168, "30d": 720}
    requests = {}
    for label, hours in request_buckets.items():
        cutoff = now - timedelta(hours=hours)
        count = (await session.execute(
            select(func.count(ApiRequestLog.request_id)).where(
                ApiRequestLog.api_key_id == key_id,
                ApiRequestLog.created_at >= cutoff,
            )
        )).scalar_one()
        requests[label] = count

    # Error counts
    error_buckets = {"1h": 1, "24h": 24, "7d": 168}
    errors = {}
    denials = {}
    for label, hours in error_buckets.items():
        cutoff = now - timedelta(hours=hours)
        err_count = (await session.execute(
            select(func.count(ApiRequestLog.request_id)).where(
                ApiRequestLog.api_key_id == key_id,
                ApiRequestLog.created_at >= cutoff,
                ApiRequestLog.status == "error",
            )
        )).scalar_one()
        errors[label] = err_count
        den_count = (await session.execute(
            select(func.count(ApiRequestLog.request_id)).where(
                ApiRequestLog.api_key_id == key_id,
                ApiRequestLog.created_at >= cutoff,
                ApiRequestLog.status == "denied",
            )
        )).scalar_one()
        denials[label] = den_count

    # Last used (from request log)
    last_used = (await session.execute(
        select(func.max(ApiRequestLog.created_at)).where(ApiRequestLog.api_key_id == key_id)
    )).scalar_one()

    # Top tools by time bucket
    top_tools = {}
    for label, hours in request_buckets.items():
        cutoff = now - timedelta(hours=hours)
        rows = (await session.execute(
            select(ApiRequestLog.endpoint, func.count(ApiRequestLog.request_id).label("cnt"))
            .where(ApiRequestLog.api_key_id == key_id, ApiRequestLog.created_at >= cutoff)
            .group_by(ApiRequestLog.endpoint)
            .order_by(func.count(ApiRequestLog.request_id).desc())
            .limit(10)
        )).all()
        top_tools[label] = [{"endpoint": r[0], "count": r[1]} for r in rows]

    return {
        "requests": requests,
        "errors": errors,
        "denials": denials,
        "last_used_at": last_used,
        "top_tools": top_tools,
    }
```

- [ ] **Step 3: Add timeline endpoint**

```python
from sqlalchemy import cast, Date as SADate


@router.get("/api-keys/{key_id}/usage/timeline")
async def get_key_timeline(
    key_id: uuid.UUID,
    days: int = 30,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Daily request counts for the timeline chart."""
    if days < 1 or days > 90:
        raise HTTPException(status_code=422, detail="days must be between 1 and 90")

    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (await session.execute(
        select(
            cast(ApiRequestLog.created_at, SADate).label("day"),
            ApiRequestLog.status,
            func.count(ApiRequestLog.request_id).label("cnt"),
        )
        .where(ApiRequestLog.api_key_id == key_id, ApiRequestLog.created_at >= cutoff)
        .group_by("day", ApiRequestLog.status)
        .order_by("day")
    )).all()

    # Pivot: group by date, sum by status
    by_day: dict[str, dict[str, int]] = {}
    for day, stat, cnt in rows:
        d = str(day)
        if d not in by_day:
            by_day[d] = {"ok": 0, "error": 0, "denied": 0}
        if stat in by_day[d]:
            by_day[d][stat] = cnt

    return [{"date": d, **counts} for d, counts in sorted(by_day.items())]
```

- [ ] **Step 4: Write tests**

Add to `tests/test_api_request_log.py`:

```python
from tests.conftest import auth_header


@pytest.mark.anyio
async def test_usage_summary_empty(client, admin_token):
    """Usage summary returns zeros when no requests logged."""
    # First create an API key
    resp = await client.post(
        "/api/api-keys",
        json={"name": "test-usage"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201
    key_id = resp.json()["key_id"]

    resp = await client.get(f"/api/api-keys/{key_id}/usage", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests"]["1h"] == 0
    assert data["requests"]["30d"] == 0
    assert data["errors"]["7d"] == 0
    assert data["denials"]["7d"] == 0
    assert data["last_used_at"] is None
    assert data["top_tools"]["30d"] == []


@pytest.mark.anyio
async def test_timeline_empty(client, admin_token):
    """Timeline returns empty list when no requests logged."""
    resp = await client.post(
        "/api/api-keys",
        json={"name": "test-timeline"},
        headers=auth_header(admin_token),
    )
    key_id = resp.json()["key_id"]

    resp = await client.get(f"/api/api-keys/{key_id}/usage/timeline?days=7", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_timeline_invalid_days(client, admin_token):
    """Timeline rejects days outside 1-90 range."""
    resp = await client.post(
        "/api/api-keys",
        json={"name": "test-timeline-invalid"},
        headers=auth_header(admin_token),
    )
    key_id = resp.json()["key_id"]

    resp = await client.get(f"/api/api-keys/{key_id}/usage/timeline?days=100", headers=auth_header(admin_token))
    assert resp.status_code == 422
```

- [ ] **Step 5: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/api/routes/api_keys.py src/harbor_clerk/api/schemas/api_keys.py
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/routes/api_keys.py src/harbor_clerk/api/schemas/api_keys.py tests/test_api_request_log.py
git commit -m "feat: usage summary + timeline endpoints for API key dashboard"
```

---

### Task 7: API Endpoints — Paginated Request Log + Purge

**Files:**
- Modify: `src/harbor_clerk/api/routes/api_keys.py`
- Test: `tests/test_api_request_log.py` (extend)

- [ ] **Step 1: Add paginated request log endpoint**

Add to `src/harbor_clerk/api/routes/api_keys.py`:

```python
@router.get("/api-keys/{key_id}/usage/requests")
async def get_key_requests(
    key_id: uuid.UUID,
    page: int = 1,
    page_size: int = 25,
    endpoint: str | None = None,
    status_filter: str | None = None,  # avoid shadowing `status` module
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Paginated request log for a specific API key."""
    if page_size > 200:
        page_size = 200
    if page < 1:
        page = 1

    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    base = select(ApiRequestLog).where(ApiRequestLog.api_key_id == key_id)
    count_base = select(func.count(ApiRequestLog.request_id)).where(ApiRequestLog.api_key_id == key_id)

    if endpoint:
        base = base.where(ApiRequestLog.endpoint == endpoint)
        count_base = count_base.where(ApiRequestLog.endpoint == endpoint)
    if status_filter:
        base = base.where(ApiRequestLog.status == status_filter)
        count_base = count_base.where(ApiRequestLog.status == status_filter)
    if from_date:
        base = base.where(ApiRequestLog.created_at >= from_date)
        count_base = count_base.where(ApiRequestLog.created_at >= from_date)
    if to_date:
        base = base.where(ApiRequestLog.created_at <= to_date)
        count_base = count_base.where(ApiRequestLog.created_at <= to_date)

    total = (await session.execute(count_base)).scalar_one()
    rows = (await session.execute(
        base.order_by(ApiRequestLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    return {
        "items": [
            {
                "request_id": str(r.request_id),
                "request_type": r.request_type,
                "endpoint": r.endpoint,
                "parameters": r.parameters,
                "status": r.status,
                "status_detail": r.status_detail,
                "result_summary": r.result_summary,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
```

- [ ] **Step 2: Add purge endpoint**

```python
import sqlalchemy as sa


@router.delete("/api-keys/{key_id}/usage")
async def purge_key_usage(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Purge all request log entries for this API key."""
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    result = await session.execute(
        sa.delete(ApiRequestLog).where(ApiRequestLog.api_key_id == key_id)
    )
    await log_audit(
        session,
        user_id=admin.id,
        action="purge_key_usage",
        target_type="api_key",
        target_id=key_id,
        detail={"deleted": result.rowcount},
    )
    await session.commit()
    return {"deleted": result.rowcount}
```

- [ ] **Step 3: Write tests**

Add to `tests/test_api_request_log.py`:

```python
@pytest.mark.anyio
async def test_request_log_paginated(client, admin_token, db_session):
    """Request log endpoint returns paginated results."""
    # Create a key
    resp = await client.post("/api/api-keys", json={"name": "test-log"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]

    # Insert some log entries directly
    for i in range(5):
        db_session.add(ApiRequestLog(
            api_key_id=uuid.UUID(key_id),
            request_type="mcp_tool",
            endpoint="kb_search",
            status="ok",
            duration_ms=i * 10,
        ))
    await db_session.commit()

    resp = await client.get(
        f"/api/api-keys/{key_id}/usage/requests?page=1&page_size=3",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 3
    assert data["page"] == 1
    assert data["page_size"] == 3


@pytest.mark.anyio
async def test_purge_key_usage(client, admin_token, db_session):
    """Purge deletes all log entries for a key."""
    resp = await client.post("/api/api-keys", json={"name": "test-purge"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]

    db_session.add(ApiRequestLog(
        api_key_id=uuid.UUID(key_id),
        request_type="mcp_tool",
        endpoint="kb_search",
        status="ok",
        duration_ms=10,
    ))
    await db_session.commit()

    resp = await client.delete(f"/api/api-keys/{key_id}/usage", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 1
```

- [ ] **Step 4: Run tests and lint**

```bash
uv run ruff check src/harbor_clerk/api/routes/api_keys.py
uv run pytest tests/test_api_request_log.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/api_keys.py tests/test_api_request_log.py
git commit -m "feat: paginated request log + purge endpoints"
```

---

### Task 8: Frontend — Route + Key Name Links

**Files:**
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/pages/ApiKeyDashboardPage.tsx` (stub)
- Modify: `frontend/src/pages/ApiKeysPage.tsx`

- [ ] **Step 1: Create stub dashboard page**

Create `frontend/src/pages/ApiKeyDashboardPage.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { get } from '../api'

interface KeyInfo {
  key_id: string
  name: string
  permission_tier: string
  scope_summary: string
  is_active: boolean
}

export default function ApiKeyDashboardPage() {
  const { keyId } = useParams<{ keyId: string }>()
  const [keyInfo, setKeyInfo] = useState<KeyInfo | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!keyId) return
    get<KeyInfo[]>('/api/api-keys')
      .then((keys) => {
        const found = keys.find((k) => k.key_id === keyId)
        if (found) setKeyInfo(found)
        else setError('API key not found')
      })
      .catch(() => setError('Failed to load key info'))
  }, [keyId])

  if (error) return <div className="text-red-500">{error}</div>
  if (!keyInfo) return <div className="text-gray-500">Loading...</div>

  return (
    <div className="animate-slide-in">
      <div className="mb-4 flex items-center gap-3">
        <Link to="/admin/keys" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">
          &larr; API Keys
        </Link>
        <h1 className="text-xl font-bold">{keyInfo.name}</h1>
        <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${
          keyInfo.is_active
            ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
            : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
        }`}>
          {keyInfo.is_active ? 'active' : 'revoked'}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400">{keyInfo.scope_summary}</span>
      </div>
      <p className="text-gray-500">Dashboard content goes here (Task 9 + 10).</p>
    </div>
  )
}
```

- [ ] **Step 2: Add route in App.tsx**

In `frontend/src/App.tsx`, add inside the `<AdminRoute>` block:

```tsx
import ApiKeyDashboardPage from './pages/ApiKeyDashboardPage'

// Add this route line:
<Route path="/admin/keys/:keyId" element={<ApiKeyDashboardPage />} />
```

- [ ] **Step 3: Make key names clickable in ApiKeysPage.tsx**

In `frontend/src/pages/ApiKeysPage.tsx`, find the key name cell (around line 285):

```tsx
<td className="px-4 py-3 text-sm font-medium">{k.name}</td>
```

Replace with:

```tsx
<td className="px-4 py-3 text-sm font-medium">
  <Link to={`/admin/keys/${k.key_id}`} className="text-blue-600 dark:text-blue-400 hover:underline">
    {k.name}
  </Link>
</td>
```

Add `import { Link } from 'react-router-dom'` at the top of the file.

- [ ] **Step 4: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/ApiKeyDashboardPage.tsx src/pages/ApiKeysPage.tsx src/App.tsx
npx tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ApiKeyDashboardPage.tsx frontend/src/pages/ApiKeysPage.tsx frontend/src/App.tsx
git commit -m "feat: API key dashboard stub page + route + key name links"
```

---

### Task 9: Frontend — Summary Cards + Charts

**Files:**
- Modify: `frontend/src/pages/ApiKeyDashboardPage.tsx`

- [ ] **Step 1: Implement summary cards and charts**

Replace the stub content in `ApiKeyDashboardPage.tsx` with the full implementation. This is a larger change — the implementer should build:

1. **Data fetching:** `GET /api/api-keys/{keyId}/usage` on mount, store in state
2. **Summary cards row:** Four cards — Requests (with dropdown: 1h/6h/12h/24h/7d/30d), Errors (dropdown: 1h/24h/7d), Denials (dropdown: 1h/24h/7d), Last Used
3. **Timeline chart:** `GET /api/api-keys/{keyId}/usage/timeline?days=30` → recharts `BarChart` with stacked bars (ok=blue, error=red, denied=amber)
4. **Tool breakdown chart:** Horizontal `BarChart` from `top_tools[selectedRange]`

**Dropdown pattern:** Each card has a `<select>` or clickable dropdown that changes a local `useState` variable. The data for all ranges is already fetched — switching the dropdown just reads a different key from the `usage` response object.

**Card styling:** Follow the existing Stats page pattern. Use `rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border)` for card containers. Red-tint errors with `bg-red-50 dark:bg-red-900/20` when count > 0, amber-tint denials with `bg-amber-50 dark:bg-amber-900/20`.

**recharts imports:**
```tsx
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
```

- [ ] **Step 2: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/ApiKeyDashboardPage.tsx
npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/ApiKeyDashboardPage.tsx
git commit -m "feat: API key dashboard summary cards + charts"
```

---

### Task 10: Frontend — Request Log Table + Purge

**Files:**
- Modify: `frontend/src/pages/ApiKeyDashboardPage.tsx`

- [ ] **Step 1: Implement request log table**

Add below the charts section:

1. **Filter bar:** Endpoint dropdown (populated from `top_tools` keys), status dropdown (all/ok/error/denied), date range inputs
2. **Table:** Time, Endpoint, Parameters (truncated to ~60 chars), Status (colored badge), Duration (ms), Result summary
3. **Server-side pagination:** Page size selector (25/50/100/200), prev/next buttons, "X of Y" indicator
4. **Expandable rows:** Click a row to reveal full `parameters` and `result_summary` JSON in a `<pre>` block below the row
5. **Error/denial styling:** Left border `border-l-2 border-red-400` for errors, `border-amber-400` for denials

**Data fetching:** `GET /api/api-keys/{keyId}/usage/requests?page=1&page_size=25&endpoint=...&status_filter=...&from=...&to=...`

Re-fetch when page, page_size, or any filter changes.

- [ ] **Step 2: Implement purge button**

Add to the header bar:

```tsx
const [confirmPurge, setConfirmPurge] = useState(false)
const [purging, setPurging] = useState(false)

// In the header:
{confirmPurge ? (
  <div className="flex items-center gap-2">
    <span className="text-xs text-red-600">Delete all logged events?</span>
    <button
      onClick={async () => {
        setPurging(true)
        try {
          await del(`/api/api-keys/${keyId}/usage`)
          setConfirmPurge(false)
          // Re-fetch data
        } catch { /* handle error */ }
        finally { setPurging(false) }
      }}
      disabled={purging}
      className="rounded-md bg-red-600 px-3 py-1 text-xs text-white hover:bg-red-700"
    >
      {purging ? 'Purging...' : 'Confirm'}
    </button>
    <button onClick={() => setConfirmPurge(false)} className="text-xs text-gray-500 hover:underline">
      Cancel
    </button>
  </div>
) : (
  <button onClick={() => setConfirmPurge(true)} className="rounded-md bg-gray-200 dark:bg-gray-700 px-3 py-1 text-xs">
    Purge Events
  </button>
)}
```

- [ ] **Step 3: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend
npx eslint src/pages/ApiKeyDashboardPage.tsx
npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ApiKeyDashboardPage.tsx
git commit -m "feat: request log table + purge button for API key dashboard"
```

---

### Task 11: Final Verification

- [ ] **Step 1: Full lint pass**

```bash
cd /Users/alex/mcp-gateway
uv run ruff check src/harbor_clerk/
uv run ruff format --check src/harbor_clerk/
cd frontend && npx eslint src/ && npx tsc --noEmit
```

- [ ] **Step 2: Run all tests**

```bash
cd /Users/alex/mcp-gateway
uv run pytest tests/ -v
```

- [ ] **Step 3: Manual smoke test**

If Harbor Clerk Server is running:
1. Open API Keys page → key names should be clickable links
2. Click a key name → dashboard page loads with zero-state cards
3. Make an MCP request with that key → refresh dashboard, see request count increment
4. Check request log table shows the tool call with correct parameters
5. Click a request row to expand parameters
6. Click Purge → confirm → log entries cleared

- [ ] **Step 4: Commit any final fixes**

```bash
git add -A
git commit -m "fix: final adjustments for API key audit dashboard"
```
