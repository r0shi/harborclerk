"""Tests for API request log endpoints (require ASGI client fixture).

Skipped on Python < 3.14 due to event loop mismatch between the test's
session-scoped engine and the ASGI client's create_app engine in CI.
"""

import sys

import pytest

from tests.conftest import auth_header

pytestmark = pytest.mark.skipif(sys.version_info < (3, 14), reason="event loop compat requires Python 3.14+")


@pytest.mark.anyio
async def test_usage_summary_empty(client, admin_token):
    """Usage summary returns zeros when no requests logged."""
    resp = await client.post("/api/api-keys", json={"name": "test-usage"}, headers=auth_header(admin_token))
    assert resp.status_code == 201
    key_id = resp.json()["key_id"]

    resp = await client.get(f"/api/api-keys/{key_id}/usage", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    # All four metric maps share the same bucket set so a single unified time-range
    # selector on the frontend can drive every panel.
    expected_buckets = {"1h", "6h", "12h", "24h", "7d", "30d"}
    for metric in ("requests", "errors", "denials", "rate_limited", "top_tools"):
        assert set(data[metric].keys()) == expected_buckets, f"{metric} missing buckets"
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
async def test_request_log_and_purge(client, admin_token):
    """Test request log pagination and purge with empty state."""
    resp = await client.post("/api/api-keys", json={"name": "test-log-p"}, headers=auth_header(admin_token))
    assert resp.status_code == 201
    key_id = resp.json()["key_id"]

    resp = await client.get(
        f"/api/api-keys/{key_id}/usage/requests?page=1&page_size=3", headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["page"] == 1
    assert data["page_size"] == 3

    # Purge (no-op on empty)
    resp = await client.delete(f"/api/api-keys/{key_id}/usage", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


@pytest.mark.anyio
async def test_request_log_pages_through_tied_timestamps_without_skipping(client, admin_token, db_session):
    """Requests logged in one transaction share created_at (now()), and the log was ordered by created_at
    alone, so its pages skipped and repeated entries. request_id is now the last sort key (#763 sweep)."""
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import text

    from harbor_clerk.models.api_request_log import ApiRequestLog

    resp = await client.post("/api/api-keys", json={"name": "test-log-ties"}, headers=auth_header(admin_token))
    key_id = resp.json()["key_id"]
    at = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(40):
        db_session.add(
            ApiRequestLog(
                api_key_id=uuid.UUID(key_id),
                request_type="mcp_tool",
                endpoint=f"kb_tool_{i:02d}",
                status="ok",
                duration_ms=1,
                created_at=at,
            )
        )
    await db_session.flush()
    # An index scan returns ties in a stable physical order and would hide the defect; a sort does not.
    await db_session.execute(text("SET LOCAL enable_indexscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    seen: list[str] = []
    for page in range(1, 8):
        resp = await client.get(
            f"/api/api-keys/{key_id}/usage/requests?page={page}&page_size=6", headers=auth_header(admin_token)
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 40
        seen.extend(item["request_id"] for item in resp.json()["items"])
    assert len(seen) == 40 and len(set(seen)) == 40, sorted(seen)
