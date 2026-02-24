"""Tests for /api/system/* endpoints."""

import pytest

from tests.conftest import auth_header


async def test_setup_status_no_users(client):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is True


async def test_setup_status_with_users(client, admin_user):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is False


async def test_health_check(client):
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["checks"]["postgres"] == "ok"
