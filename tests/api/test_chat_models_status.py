"""Tests for /api/chat/models/status — focuses on the `model_name` field
added so the Observatory's summarize card can render the human-readable
LLM name without re-fetching the full model list.
"""

from unittest.mock import patch

import httpx
import pytest

from harbor_clerk.config import get_settings
from tests.conftest import auth_header


@pytest.fixture
def _set_llm_model_id(monkeypatch):
    """Set settings.llm_model_id for a single test.

    Also no-ops refresh_llm_settings so our monkey-patched value isn't
    overwritten by a real config.json on disk during the test.
    """

    def _set(model_id: str | None):
        settings = get_settings()
        monkeypatch.setattr(settings, "llm_model_id", model_id or "")
        monkeypatch.setattr("harbor_clerk.api.routes.chat.refresh_llm_settings", lambda: None)

    return _set


@pytest.fixture
def _fail_llama_probe():
    """Make the in-endpoint llama-server probe raise ConnectError.

    Patches httpx.AsyncClient.get with a side_effect that only raises
    when called against the llama_server_url — the test's own httpx
    AsyncClient (used to call the endpoint via ASGITransport) is left
    alone so it can still drive the request through.
    """
    settings = get_settings()
    llama_url_prefix = settings.llama_server_url
    real_get = httpx.AsyncClient.get

    async def _selective(self, url, *args, **kwargs):
        if isinstance(url, str) and url.startswith(llama_url_prefix):
            raise httpx.ConnectError("boom")
        return await real_get(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "get", _selective):
        yield


@pytest.fixture
def _succeed_llama_probe():
    """Make the in-endpoint llama-server probe return 200.

    Mirror of _fail_llama_probe but with a synthesised healthy response,
    so we exercise the `ready` return branch without needing a real
    llama-server in the test environment.
    """
    settings = get_settings()
    llama_url_prefix = settings.llama_server_url
    real_get = httpx.AsyncClient.get

    async def _selective(self, url, *args, **kwargs):
        if isinstance(url, str) and url.startswith(llama_url_prefix):
            return httpx.Response(200, content=b'{"status":"ok"}')
        return await real_get(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "get", _selective):
        yield


@pytest.fixture
def _force_afm(monkeypatch):
    """Toggle settings.summary_force_apple_intelligence for one test."""

    def _set(enabled: bool):
        settings = get_settings()
        monkeypatch.setattr(settings, "summary_force_apple_intelligence", enabled)

    return _set


@pytest.fixture
def _afm_binary(monkeypatch):
    """Stub _find_apple_summarize_binary to control AFM availability per test."""

    def _set(available: bool):
        monkeypatch.setattr(
            "harbor_clerk.llm.summarize._find_apple_summarize_binary",
            lambda: "/fake/apple-summarize" if available else None,
        )

    return _set


async def test_status_returns_model_name_for_known_model(
    client, admin_user, admin_token, _set_llm_model_id, _fail_llama_probe, _afm_binary
):
    _afm_binary(False)
    _set_llm_model_id("qwen3-8b")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "loading"
    assert body["model_id"] == "qwen3-8b"
    assert body["model_name"] == "Qwen3 8B"
    # LLM is the configured summarize backend; mirrors the llama-server state.
    assert body["summarize"] == {"backend": "qwen3-8b", "name": "Qwen3 8B", "state": "loading"}


async def test_status_returns_null_model_name_when_deactivated(
    client, admin_user, admin_token, _set_llm_model_id, _afm_binary
):
    _afm_binary(False)
    _set_llm_model_id(None)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "deactivated"
    assert body["model_id"] is None
    assert body["model_name"] is None
    # No LLM + no AFM → extractive is the (degraded) summarize backend.
    assert body["summarize"]["backend"] == "extractive"
    assert body["summarize"]["state"] == "ready"


async def test_status_returns_null_model_name_for_unknown_model_id(
    client, admin_user, admin_token, _set_llm_model_id, _fail_llama_probe, _afm_binary
):
    _afm_binary(False)
    _set_llm_model_id("not-a-real-model-id")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    # The id is preserved as the source of truth; only the human label is null.
    assert body["model_id"] == "not-a-real-model-id"
    assert body["model_name"] is None


async def test_status_returns_ready_with_model_name_when_llama_healthy(
    client, admin_user, admin_token, _set_llm_model_id, _succeed_llama_probe, _afm_binary
):
    _afm_binary(False)
    _set_llm_model_id("qwen3-8b")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["model_id"] == "qwen3-8b"
    assert body["model_name"] == "Qwen3 8B"
    assert body["summarize"] == {"backend": "qwen3-8b", "name": "Qwen3 8B", "state": "ready"}


async def test_summarize_backend_is_afm_when_force_afm_and_binary_available(
    client, admin_user, admin_token, _set_llm_model_id, _succeed_llama_probe, _force_afm, _afm_binary
):
    """When force-AFM is on and the binary is found, summarize reports AFM regardless of the LLM state."""
    _set_llm_model_id("qwen3-8b")
    _force_afm(True)
    _afm_binary(True)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    body = resp.json()
    # LLM probe still drives the top-level state (other stages still use the LLM).
    assert body["state"] == "ready"
    assert body["model_id"] == "qwen3-8b"
    # Summarize stage is AFM-backed.
    assert body["summarize"] == {"backend": "apple-intelligence", "name": "Apple Intelligence", "state": "ready"}


async def test_summarize_backend_reports_unavailable_when_force_afm_but_binary_missing(
    client, admin_user, admin_token, _set_llm_model_id, _force_afm, _afm_binary
):
    """force-AFM on + binary missing reports the configured backend as unavailable."""
    _set_llm_model_id(None)
    _force_afm(True)
    _afm_binary(False)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    body = resp.json()
    assert body["summarize"]["backend"] == "apple-intelligence"
    assert body["summarize"]["state"] == "loading"
    assert "unavailable" in body["summarize"]["name"].lower()


async def test_summarize_backend_is_afm_when_no_llm_but_binary_available(
    client, admin_user, admin_token, _set_llm_model_id, _force_afm, _afm_binary
):
    """No LLM configured + AFM available → AFM is the (default-fallback) summarize backend."""
    _set_llm_model_id(None)
    _force_afm(False)
    _afm_binary(True)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    body = resp.json()
    assert body["state"] == "deactivated"
    assert body["summarize"]["backend"] == "apple-intelligence"
    assert body["summarize"]["state"] == "ready"


# --- memory budget (#556) ---------------------------------------------------------------------------------------


@pytest.fixture
def _small_mac(monkeypatch, tmp_path):
    """An 8 GB Mac with every model downloaded, and no side effects on
    activation (no config.json write, no llama-server restart)."""
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 8_000_000_000)
    monkeypatch.setattr(
        "harbor_clerk.api.routes.chat.get_model_path", lambda model_id: str(tmp_path / f"{model_id}.gguf")
    )
    monkeypatch.setattr("harbor_clerk.api.routes.chat.list_downloaded", lambda: ["qwen3-4b", "qwen36-35b-a3b"])
    monkeypatch.setattr("harbor_clerk.api.routes.chat.sync_native_config", lambda key, value: None)
    monkeypatch.setattr("harbor_clerk.api.routes.chat.refresh_llm_settings", lambda: None)
    monkeypatch.setattr("harbor_clerk.llm.health.request_llm_restart", lambda reason: None)


@pytest.mark.asyncio
async def test_model_list_says_what_fits_this_mac(client, admin_token, _small_mac):
    resp = await client.get("/api/chat/models", headers=auth_header(admin_token))
    assert resp.status_code == 200
    by_id = {m["id"]: m for m in resp.json()}
    small, heavy = by_id["qwen3-4b"], by_id["qwen36-35b-a3b"]
    assert small["min_ram_gb"] == 16 and small["max_context_here"] == 0 and small["fits_here"] is False
    assert heavy["min_ram_gb"] == 36 and heavy["max_context_here"] == 0
    assert heavy["memory_bytes"] > 28_000_000_000 and heavy["system_ram_gb"] == 7.5, "8e9 bytes is 7.5 GiB"
    plenty = {m["id"] for m in resp.json() if m["fits_here"]}
    assert plenty == set(), "nothing fits an 8 GB Mac with the rest of the app resident"


@pytest.mark.asyncio
async def test_activating_a_model_this_mac_cannot_hold_is_refused_with_no_override(client, admin_token, _small_mac):
    """No force flag: the macOS launcher applies the same arithmetic and would
    refuse to start it, so an override here would activate a model that never runs."""
    refused = await client.put("/api/chat/models/qwen36-35b-a3b/activate", headers=auth_header(admin_token))
    assert refused.status_code == 409
    assert "needs about 36 GB" in refused.json()["detail"] and "this Mac has 7.5 GB" in refused.json()["detail"]
    forced = await client.put("/api/chat/models/qwen36-35b-a3b/activate?force=true", headers=auth_header(admin_token))
    assert forced.status_code == 409, "an unknown query parameter changes nothing"


@pytest.mark.asyncio
async def test_activating_a_model_that_fits_at_a_smaller_context_is_allowed(
    client, admin_token, _small_mac, monkeypatch
):
    """The launcher clamps the context; the API does not stand in the way."""
    from harbor_clerk.llm.models import MODELS, max_context

    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 12_000_000_000)
    assert 0 < max_context(MODELS["qwen3-4b"], 12_000_000_000) < MODELS["qwen3-4b"].context_window
    resp = await client.put("/api/chat/models/qwen3-4b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    listed = await client.get("/api/chat/models", headers=auth_header(admin_token))
    small = next(m for m in listed.json() if m["id"] == "qwen3-4b")
    assert small["fits_here"] is False and 0 < small["max_context_here"] < 32768


@pytest.mark.asyncio
async def test_unknown_memory_refuses_nothing_and_claims_nothing(client, admin_token, _small_mac, monkeypatch):
    """Off macOS the page size or page count may not be readable. A gate on a 0 GB Mac would refuse every model."""
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 0)
    resp = await client.put("/api/chat/models/qwen36-35b-a3b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    listed = await client.get("/api/chat/models", headers=auth_header(admin_token))
    heavy = next(m for m in listed.json() if m["id"] == "qwen36-35b-a3b")
    assert heavy["fits_here"] is True and heavy["max_context_here"] == 262144 and heavy["system_ram_gb"] == 0.0


@pytest.mark.asyncio
async def test_the_list_reports_the_context_the_launcher_will_ask_for_when_yarn_is_on(
    client, admin_token, _small_mac, monkeypatch, _set_llm_model_id
):
    """The launcher requests and clamps the YaRN window; the page said the plain window fit with no clamp."""
    from harbor_clerk.config import get_settings

    _set_llm_model_id("")
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(get_settings(), "llm_yarn_enabled", True)
    resp = await client.get("/api/chat/models", headers=auth_header(admin_token))
    q8 = next(m for m in resp.json() if m["id"] == "qwen3-8b")
    assert q8["max_context_here"] == 22528 and q8["fits_here"] is False, (
        "the 131072 YaRN window, clamped as the launcher clamps it: a tenth of the Mac stays free (#684)"
    )
    assert q8["min_ram_gb"] == 36, "at 131072 tokens it is a 36 GB-tier model, not 16"
    monkeypatch.setattr(get_settings(), "llm_yarn_enabled", False)
    plain = next(
        m
        for m in (await client.get("/api/chat/models", headers=auth_header(admin_token))).json()
        if m["id"] == "qwen3-8b"
    )
    # YaRN off, the clamp is the same one: this Mac was never short of the plain window by much, and is now.
    assert plain["max_context_here"] == 22528 and plain["fits_here"] is False and plain["min_ram_gb"] == 18
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 18 * 1024**3)
    roomy = next(
        m
        for m in (await client.get("/api/chat/models", headers=auth_header(admin_token))).json()
        if m["id"] == "qwen3-8b"
    )
    assert roomy["max_context_here"] == 32768 and roomy["fits_here"] is True
