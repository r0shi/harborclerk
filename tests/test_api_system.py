"""Tests for /api/system/* endpoints."""


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


async def test_health_check_exposes_allow_source_download_default_false(client):
    """The health endpoint surfaces the allow_source_download capability so the
    frontend can decide whether to render the Download button. Default must be
    False on every deployment — see the setting docstring for why."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "allow_source_download" in data
    assert data["allow_source_download"] is False


async def test_health_check_reflects_allow_source_download_when_set(client):
    """Toggling the setting at runtime should be visible immediately on the
    next health fetch. Tests use the same Settings singleton; mutating it on
    the fly is the production-equivalent of an admin flipping the env var
    and restarting (the singleton is read fresh on each request)."""
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.allow_source_download
    s.allow_source_download = True
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        assert resp.json()["allow_source_download"] is True
    finally:
        s.allow_source_download = original
