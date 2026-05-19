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


async def test_status_returns_model_name_for_known_model(
    client, admin_user, admin_token, _set_llm_model_id, _fail_llama_probe
):
    _set_llm_model_id("qwen3-8b")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "loading"
    assert body["model_id"] == "qwen3-8b"
    assert body["model_name"] == "Qwen3 8B"


async def test_status_returns_null_model_name_when_deactivated(client, admin_user, admin_token, _set_llm_model_id):
    _set_llm_model_id(None)
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "deactivated"
    assert body["model_id"] is None
    assert body["model_name"] is None


async def test_status_returns_null_model_name_for_unknown_model_id(
    client, admin_user, admin_token, _set_llm_model_id, _fail_llama_probe
):
    _set_llm_model_id("not-a-real-model-id")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    # The id is preserved as the source of truth; only the human label is null.
    assert body["model_id"] == "not-a-real-model-id"
    assert body["model_name"] is None


async def test_status_returns_ready_with_model_name_when_llama_healthy(
    client, admin_user, admin_token, _set_llm_model_id, _succeed_llama_probe
):
    _set_llm_model_id("qwen3-8b")
    resp = await client.get("/api/chat/models/status", headers=auth_header(admin_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["model_id"] == "qwen3-8b"
    assert body["model_name"] == "Qwen3 8B"
