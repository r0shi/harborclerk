"""Tests for CLI access gate in MCPAuthMiddleware.

The gate rejects requests with User-Agent: harbor-clerk-cli/* when
ENABLE_CLI_ACCESS=false (the default), and passes them through when true.
Non-CLI user-agents are never affected by the toggle.

These tests operate at the ASGI middleware level (no real DB), using the
same mock-_resolve_principal pattern as test_mcp_auth.py.
"""

import json
import uuid

import pytest

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import MCPAuthMiddleware

# ---------------------------------------------------------------------------
# Helpers (copied from test_mcp_auth.py pattern)
# ---------------------------------------------------------------------------

_VALID_KEY = "hc_test1234567890abcdef"
_ADMIN_PRINCIPAL = Principal(type="api_key", id=uuid.uuid4(), role="admin")


async def _echo_app(scope, receive, send):
    """Minimal ASGI inner app that returns 200."""
    body = b'{"result": "ok"}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _capture_response(app, scope):
    """Invoke an ASGI app and return (status, headers_dict, body_bytes)."""
    status = None
    headers = {}
    body_parts = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, headers, b"".join(body_parts)


def _http_scope(path="/mcp", headers=None):
    """Build a minimal HTTP ASGI scope."""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }


def _headers_with(auth_key: str, user_agent: str | None = None) -> list:
    h = [(b"authorization", f"Bearer {auth_key}".encode())]
    if user_agent is not None:
        h.append((b"user-agent", user_agent.encode()))
    return h


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCliAccessGate:
    @pytest.fixture
    def app(self, monkeypatch):
        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)
        return MCPAuthMiddleware(_echo_app)

    @pytest.mark.asyncio
    async def test_cli_request_rejected_when_disabled(self, app, monkeypatch):
        """CLI UA is blocked with 403 when ENABLE_CLI_ACCESS=false."""
        monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
        # Force re-read by patching settings directly
        from harbor_clerk import config as _config

        _config._settings = None

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="harbor-clerk-cli/0.1.0"))
        status, _, body = await _capture_response(app, scope)
        assert status == 403
        data = json.loads(body)
        assert data["error"] == "cli_access_disabled"
        assert "Integrations" in data["hint"]

    @pytest.mark.asyncio
    async def test_cli_request_allowed_when_enabled(self, app, monkeypatch):
        """CLI UA passes through when ENABLE_CLI_ACCESS=true."""
        monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
        from harbor_clerk import config as _config

        _config._settings = None

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="harbor-clerk-cli/0.1.0"))
        status, _, body = await _capture_response(app, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_mcp_request_unaffected_by_cli_toggle(self, app, monkeypatch):
        """Regular MCP clients (Claude.ai, etc.) pass through regardless of toggle."""
        monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
        from harbor_clerk import config as _config

        _config._settings = None

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="Claude/1.0"))
        status, _, body = await _capture_response(app, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_no_user_agent_unaffected(self, app, monkeypatch):
        """Requests with no User-Agent header are not CLI and pass through."""
        monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
        from harbor_clerk import config as _config

        _config._settings = None

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent=None))
        status, _, body = await _capture_response(app, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_cli_is_cli_contextvar_set_when_allowed(self, app, monkeypatch):
        """When CLI access is enabled, _mcp_is_cli contextvar is True inside the inner app."""
        from harbor_clerk.mcp_server import _mcp_is_cli

        monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
        from harbor_clerk import config as _config

        _config._settings = None

        captured_is_cli = []

        async def capture_app(scope, receive, send):
            captured_is_cli.append(_mcp_is_cli.get())
            body = b'{"result": "ok"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)
        gated_app = MCPAuthMiddleware(capture_app)

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="harbor-clerk-cli/1.2.3"))
        await _capture_response(gated_app, scope)
        assert captured_is_cli == [True]
        # Cleaned up after request
        assert _mcp_is_cli.get() is False

    @pytest.mark.asyncio
    async def test_non_cli_is_cli_contextvar_false(self, app, monkeypatch):
        """For non-CLI requests, _mcp_is_cli is False inside the inner app."""
        from harbor_clerk.mcp_server import _mcp_is_cli

        monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
        from harbor_clerk import config as _config

        _config._settings = None

        captured_is_cli = []

        async def capture_app(scope, receive, send):
            captured_is_cli.append(_mcp_is_cli.get())
            body = b'{"result": "ok"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)
        gated_app = MCPAuthMiddleware(capture_app)

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="Claude/1.0"))
        await _capture_response(gated_app, scope)
        assert captured_is_cli == [False]

    @pytest.mark.asyncio
    async def test_cli_gate_honors_native_config_without_restart(self, app, monkeypatch, tmp_path):
        """Writing enable_cli_access=true to a temp config.json allows the CLI
        without a process restart.  Mirrors the runtime-refresh pattern used on
        macOS, where the user toggles the setting in Preferences."""
        config_file = tmp_path / "config.json"
        config_file.write_text('{"enable_cli_access": false}\n')

        monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
        monkeypatch.setenv("NATIVE_CONFIG_FILE", str(config_file))
        from harbor_clerk import config as _config

        _config._settings = None

        scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="harbor-clerk-cli/0.1.0"))

        # Disabled via config.json → 403
        status, _, body = await _capture_response(app, scope)
        assert status == 403
        data = json.loads(body)
        assert data["error"] == "cli_access_disabled"

        # Simulate macOS Preferences toggle: write true without restarting
        config_file.write_text('{"enable_cli_access": true}\n')

        # Next request sees the new value without resetting _settings
        status, _, _ = await _capture_response(app, scope)
        assert status == 200

    @pytest.mark.asyncio
    async def test_cli_gate_denial_writes_audit_row(self, app, monkeypatch):
        """When CLI access is disabled, the gate writes an audit row with the
        correct request_type, endpoint, status, status_detail, and api_key_id."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
        from harbor_clerk import config as _config

        _config._settings = None

        mock_log = AsyncMock()

        # The gate opens a DB session purely to call log_api_request; replace
        # both so the test needs no real database connection.
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_session_factory():
            yield mock_session

        with (
            patch("harbor_clerk.api.request_log.log_api_request", mock_log),
            patch("harbor_clerk.mcp_server.async_session_factory", mock_session_factory),
        ):
            scope = _http_scope(headers=_headers_with(_VALID_KEY, user_agent="harbor-clerk-cli/0.1.0"))
            status, _, _ = await _capture_response(app, scope)

        assert status == 403
        mock_log.assert_called_once()
        _, kwargs = mock_log.call_args
        assert kwargs["request_type"] == "cli_tool"
        assert kwargs["endpoint"] == "<gate>"
        assert kwargs["status"] == "denied"
        assert kwargs["status_detail"] == "cli_access_disabled"
        assert kwargs["api_key_id"] == _ADMIN_PRINCIPAL.id
