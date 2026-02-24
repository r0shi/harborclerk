"""Tests for /api/auth/* and /me endpoints."""

import pytest

from tests.conftest import auth_header


async def test_login_success(client, admin_user):
    resp = await client.post("/api/auth/login", json={
        "email": "admin@test.com",
        "password": "TestPassword123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["email"] == "admin@test.com"


async def test_login_wrong_password(client, admin_user):
    resp = await client.post("/api/auth/login", json={
        "email": "admin@test.com",
        "password": "WrongPassword999",
    })
    assert resp.status_code == 401


async def test_login_nonexistent_user(client):
    resp = await client.post("/api/auth/login", json={
        "email": "nobody@test.com",
        "password": "Whatever123!",
    })
    assert resp.status_code == 401


async def test_get_me_with_token(client, admin_user, admin_token):
    resp = await client.get("/api/me", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "admin@test.com"
    assert data["role"] == "admin"


async def test_get_me_without_token(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_logout(client):
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
