import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.middleware import _normalize_path
from harbor_clerk.api.request_log import log_api_request
from harbor_clerk.models.api_request_log import ApiRequestLog
from tests.conftest import auth_header


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
    assert row.api_key_id is None
    assert row.duration_ms == 55


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


# ---------------------------------------------------------------------------
# Usage / Audit dashboard endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_usage_summary_empty(client, admin_token):
    resp = await client.post("/api/api-keys", json={"name": "test-usage"}, headers=auth_header(admin_token))
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
    resp = await client.post("/api/api-keys", json={"name": "test-tl"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]
    resp = await client.get(f"/api/api-keys/{key_id}/usage/timeline?days=7", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_timeline_invalid_days(client, admin_token):
    resp = await client.post("/api/api-keys", json={"name": "test-tl2"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]
    resp = await client.get(f"/api/api-keys/{key_id}/usage/timeline?days=100", headers=auth_header(admin_token))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_request_log_paginated(client, admin_token, db_session):
    resp = await client.post("/api/api-keys", json={"name": "test-log"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]
    for i in range(5):
        db_session.add(
            ApiRequestLog(
                api_key_id=uuid.UUID(key_id),
                request_type="mcp_tool",
                endpoint="kb_search",
                status="ok",
                duration_ms=i * 10,
            )
        )
    await db_session.commit()

    resp = await client.get(
        f"/api/api-keys/{key_id}/usage/requests?page=1&page_size=3", headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 3


@pytest.mark.anyio
async def test_purge_key_usage(client, admin_token, db_session):
    resp = await client.post("/api/api-keys", json={"name": "test-purge"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]
    db_session.add(
        ApiRequestLog(
            api_key_id=uuid.UUID(key_id),
            request_type="mcp_tool",
            endpoint="kb_search",
            status="ok",
            duration_ms=10,
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/api-keys/{key_id}/usage", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["deleted"] >= 1
