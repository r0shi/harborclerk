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


async def test_summarize_backend_falls_back_to_extractive_when_force_afm_but_binary_missing(
    client, admin_user, admin_token, _set_llm_model_id, _force_afm, _afm_binary
):
    """force-AFM on + binary missing → extractive label (matches generate_summary's runtime behavior)."""
    _set_llm_model_id(None)
    _force_afm(True)
    _afm_binary(False)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    body = resp.json()
    assert body["summarize"]["backend"] == "extractive"
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
