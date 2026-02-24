"""Tests for /api/setup endpoint."""

import pytest

from tests.conftest import auth_header


async def test_setup_creates_admin(client):
    resp = await client.post("/api/setup", json={
        "email": "admin@test.com",
        "password": "StrongPassword123!",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["email"] == "admin@test.com"
    assert data["user"]["role"] == "admin"


async def test_setup_rejects_weak_password(client):
    resp = await client.post("/api/setup", json={
        "email": "admin@test.com",
        "password": "weak",
    })
    assert resp.status_code == 422


async def test_setup_rejects_when_users_exist(client, admin_user):
    resp = await client.post("/api/setup", json={
        "email": "another@test.com",
        "password": "StrongPassword123!",
    })
    assert resp.status_code == 409
