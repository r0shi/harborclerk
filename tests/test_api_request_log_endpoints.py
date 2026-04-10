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
