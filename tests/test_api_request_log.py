"""Tests for API request log model and helper (no ASGI client needed).

The async DB tests are skipped on Python < 3.14 due to event loop
mismatch between the session-scoped engine and asyncpg in CI (3.12).
Path normalization tests (pure functions) always run.
"""

import sys

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.middleware import _normalize_path
from harbor_clerk.api.request_log import log_api_request
from harbor_clerk.api.scope import KeyScope
from harbor_clerk.models.api_request_log import ApiRequestLog

_skip_312 = pytest.mark.skipif(sys.version_info < (3, 14), reason="event loop compat requires Python 3.14+")


@_skip_312
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


@_skip_312
@pytest.mark.anyio
async def test_log_api_request_helper(db_session: AsyncSession):
    """log_api_request creates an entry with all fields."""
    await log_api_request(
        db_session,
        api_key_id=None,
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
    assert row.duration_ms == 55


@_skip_312
@pytest.mark.anyio
async def test_log_api_request_denied(db_session: AsyncSession):
    """Denied requests are logged with status_detail."""
    await log_api_request(
        db_session,
        api_key_id=None,
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


def test_normalize_path_with_uuid():
    assert _normalize_path("GET", "/api/docs/550e8400-e29b-41d4-a716-446655440000") == "GET /api/docs/{id}"


def test_normalize_path_without_uuid():
    assert _normalize_path("GET", "/api/api-keys") == "GET /api/api-keys"


def test_normalize_path_multiple_uuids():
    result = _normalize_path(
        "GET", "/api/docs/550e8400-e29b-41d4-a716-446655440000/versions/660e8400-e29b-41d4-a716-446655440001"
    )
    assert result == "GET /api/docs/{id}/versions/{id}"


@_skip_312
@pytest.mark.anyio
async def test_mcp_call_tool_denied_is_logged(_engine, db_session, admin_user):
    """ScopedFastMCP.call_tool logs denied tool calls to api_request_log."""
    from unittest.mock import patch

    from mcp.server.fastmcp.exceptions import ToolError
    from sqlalchemy.ext.asyncio import AsyncSession as AS
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harbor_clerk.auth import generate_api_key, hash_api_key
    from harbor_clerk.mcp_server import _mcp_principal, mcp
    from harbor_clerk.models import ApiKey

    test_factory = async_sessionmaker(_engine, class_=AS, expire_on_commit=False)

    raw_key = generate_api_key()
    api_key = ApiKey(name="test-denied", key_hash=hash_api_key(raw_key))
    db_session.add(api_key)
    await db_session.flush()
    key_id = api_key.key_id
    await db_session.commit()

    principal = Principal(
        type="api_key",
        id=key_id,
        role="user",
        key_scope=KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=None,
            permission_tier="search",
            tool_overrides={},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        ),
    )

    with patch("harbor_clerk.mcp_server.async_session_factory", test_factory):
        token = _mcp_principal.set(principal)
        try:
            with pytest.raises(ToolError):
                await mcp.call_tool("kb_read_document", {})
        finally:
            _mcp_principal.reset(token)

    async with test_factory() as fresh:
        result = await fresh.execute(
            select(ApiRequestLog).where(
                ApiRequestLog.api_key_id == key_id,
                ApiRequestLog.status == "denied",
            )
        )
        row = result.scalar_one_or_none()
    assert row is not None, "Denied tool call was not logged to api_request_log"
    assert row.endpoint == "kb_read_document"
    assert "scope" in row.status_detail or "tier" in row.status_detail
