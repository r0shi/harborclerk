"""Tests for /api/auth/* and /me endpoints."""

from tests.conftest import auth_header


async def test_login_success(client, admin_user):
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": "admin@test.com",
            "password": "TestPassword123",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["user"]["email"] == "admin@test.com"


async def test_login_wrong_password(client, admin_user):
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": "admin@test.com",
            "password": "WrongPassword999",
        },
    )
    assert resp.status_code == 401


async def test_login_nonexistent_user(client):
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": "nobody@test.com",
            "password": "Whatever123!",
        },
    )
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


async def test_update_preferences_persists_onboarding_complete(client, admin_user, admin_token):
    """PATCH /api/me/preferences must store onboardingComplete in JSONB.

    Regression for the wizard-can't-be-dismissed bug: PreferencesUpdate's
    Pydantic model previously only declared `theme` and `page_size`, so
    Pydantic silently stripped `onboardingComplete` from the request body.
    The wizard's onComplete handler succeeded but the flag never reached the
    database, so Layout.tsx kept rendering the wizard.
    """
    # Initial PATCH writes the flag
    resp = await client.patch(
        "/api/me/preferences",
        json={"onboardingComplete": True},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["preferences"].get("onboardingComplete") is True

    # GET /api/me confirms persistence (catches the case where the response
    # is correct but the database write was a no-op).
    resp = await client.get("/api/me", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["preferences"].get("onboardingComplete") is True


async def test_update_preferences_partial_does_not_clobber_existing(client, admin_user, admin_token):
    """A subsequent PATCH that only sends `theme` must preserve onboardingComplete."""
    # First, set onboardingComplete = true
    resp = await client.patch(
        "/api/me/preferences",
        json={"onboardingComplete": True},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200

    # Then, update only theme — onboardingComplete should remain
    resp = await client.patch(
        "/api/me/preferences",
        json={"theme": "dark"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    prefs = resp.json()["preferences"]
    assert prefs.get("theme") == "dark"
    assert prefs.get("onboardingComplete") is True


async def test_update_preferences_enabled_languages_persists(client, admin_user, admin_token):
    resp = await client.patch(
        "/api/me/preferences",
        json={"enabled_languages": ["en", "fr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["preferences"]["enabled_languages"] == ["en", "fr"]


async def test_update_preferences_enabled_languages_always_includes_english(client, admin_user, admin_token):
    """Operator who tries to set ['fr'] (without en) gets ['en', 'fr'] back —
    English is the bundled fallback and turning it off would silently
    disable OCR for every doc."""
    resp = await client.patch(
        "/api/me/preferences",
        json={"enabled_languages": ["fr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    enabled = resp.json()["preferences"]["enabled_languages"]
    assert enabled[0] == "en"
    assert "fr" in enabled


async def test_update_preferences_enabled_languages_dedupes(client, admin_user, admin_token):
    resp = await client.patch(
        "/api/me/preferences",
        json={"enabled_languages": ["en", "fr", "en", "fr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["preferences"]["enabled_languages"] == ["en", "fr"]


async def test_update_preferences_enabled_languages_rejects_unknown_codes(client, admin_user, admin_token):
    resp = await client.patch(
        "/api/me/preferences",
        json={"enabled_languages": ["en", "xx"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422
    assert "xx" in resp.json()["detail"]
