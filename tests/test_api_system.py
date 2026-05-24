"""Tests for /api/system/* endpoints."""

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


async def test_health_check_exposes_allow_source_download_default_false(client):
    """The health endpoint surfaces the allow_source_download capability so the
    frontend can decide whether to render the Download button. Default must be
    False on every deployment — see the setting docstring for why."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "allow_source_download" in data
    assert data["allow_source_download"] is False


async def test_health_check_exposes_enable_cli_access_default_false(client):
    """The health endpoint surfaces the enable_cli_access capability so the
    frontend Integrations page can show whether CLI access is enabled. Default
    must be False on every deployment — it is an opt-in feature."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "enable_cli_access" in data
    assert data["enable_cli_access"] is False


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


async def test_summary_backlog_endpoint_returns_all_four_fields(client, admin_user, admin_token):
    """The Observatory Summary Backlog widget needs depth, throughput,
    p50, and depth-over-time history. Endpoint must return all four."""
    response = await client.get("/api/system/summary-backlog", headers=auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()
    assert "queue_depth" in data
    assert "throughput_per_min" in data
    assert "p50_seconds" in data
    assert "depth_history" in data
    assert isinstance(data["depth_history"], list)
    if data["depth_history"]:
        assert len(data["depth_history"][0]) == 2
    # 5-minute samples over the last hour = 13 buckets
    assert len(data["depth_history"]) == 13
    # Type + range checks: regressions like queue_depth=null, p50="N/A",
    # or throughput as a string would all pass mere presence checks.
    assert isinstance(data["queue_depth"], int) and data["queue_depth"] >= 0
    assert isinstance(data["throughput_per_min"], (int, float)) and data["throughput_per_min"] >= 0
    assert isinstance(data["p50_seconds"], (int, float)) and data["p50_seconds"] >= 0
    assert all(isinstance(ts, (int, float)) for ts, _ in data["depth_history"])
    assert all(isinstance(d, int) and d >= 0 for _, d in data["depth_history"])
