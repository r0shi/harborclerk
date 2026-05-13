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


async def test_password_change_revokes_existing_access_token(client, admin_user, admin_token):
    """Regression: an access token issued before a password change must
    be rejected after the change. Closes the security gap where an
    exfiltrated token survived a rotation done to revoke it.

    The implementation compares JWT `iat` to `users.password_changed_at`
    in the deps layer. To avoid a wall-clock race in tests (we'd need
    the password change to happen at least 1s after `admin_token` is
    minted to clear the 1s clock-skew grace), explicitly backdate the
    admin_token by minting it ourselves with an earlier `iat`.
    """
    import time

    from harbor_clerk.auth import create_access_token

    # Mint an access token, then sleep past the 1s grace window before
    # changing the password so the grace can't mask a real bug.
    old_token = create_access_token(admin_user.user_id, admin_user.role.value)

    # Confirm the old token works pre-change.
    resp = await client.get("/api/me", headers=auth_header(old_token))
    assert resp.status_code == 200

    time.sleep(1.2)

    # Change password (uses the still-valid token).
    resp = await client.post(
        "/api/me/password",
        headers=auth_header(old_token),
        json={"current_password": "TestPassword123", "new_password": "NewPassword456!"},
    )
    assert resp.status_code == 204, resp.text

    # Old token now rejected.
    resp = await client.get("/api/me", headers=auth_header(old_token))
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()

    # Logging in with the new password yields a fresh token that works.
    resp = await client.post(
        "/api/auth/login",
        json={"email": "admin@test.com", "password": "NewPassword456!"},
    )
    assert resp.status_code == 200
    fresh_token = resp.json()["access_token"]
    resp = await client.get("/api/me", headers=auth_header(fresh_token))
    assert resp.status_code == 200


async def test_password_change_revokes_existing_refresh_token(client, admin_user):
    """Regression: a refresh token cookie issued before a password change
    must fail to mint a new access token after the change."""
    import time

    # Login to get a refresh cookie + access token.
    resp = await client.post(
        "/api/auth/login",
        json={"email": "admin@test.com", "password": "TestPassword123"},
    )
    assert resp.status_code == 200
    access_token = resp.json()["access_token"]
    # httpx test client stores cookies on the client itself; the refresh
    # cookie is now in client.cookies and will be auto-sent on subsequent
    # requests to /api/auth/refresh.

    # Sleep past the 1s clock-skew grace.
    time.sleep(1.2)

    # Change password using the still-valid access token.
    resp = await client.post(
        "/api/me/password",
        headers=auth_header(access_token),
        json={"current_password": "TestPassword123", "new_password": "NewPassword456!"},
    )
    assert resp.status_code == 204, resp.text

    # Old refresh cookie should now be rejected.
    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()


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
