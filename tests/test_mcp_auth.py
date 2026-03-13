"""Tests for MCP auth middleware (header-based and URL-token-based)."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import MCPAuthMiddleware, MCPTokenPathAuth, _mcp_principal, _resolve_principal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_KEY = "hc_test1234567890abcdef"
_ADMIN_PRINCIPAL = Principal(type="api_key", id=uuid.uuid4(), role="admin")


async def _echo_app(scope, receive, send):
    """Minimal ASGI app that echoes back the path and principal."""
    principal = _mcp_principal.get()
    body = json.dumps(
        {
            "path": scope.get("path", ""),
            "principal_type": principal.type if principal else None,
            "principal_role": principal.role if principal else None,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"], [b"content-length", str(len(body)).encode()]],
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


def _http_scope(path="/", headers=None):
    """Build a minimal HTTP ASGI scope."""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }


# ---------------------------------------------------------------------------
# MCPAuthMiddleware tests
# ---------------------------------------------------------------------------


class TestMCPAuthMiddleware:
    @pytest.fixture
    def app(self):
        return MCPAuthMiddleware(_echo_app)

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self, app, monkeypatch):
        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        scope = _http_scope(headers=[(b"authorization", f"Bearer {_VALID_KEY}".encode())])
        status, _, body = await _capture_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["principal_type"] == "api_key"
        assert data["principal_role"] == "admin"

    @pytest.mark.asyncio
    async def test_missing_auth_header(self, app, monkeypatch):
        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", lambda t: None)

        scope = _http_scope()
        status, _, body = await _capture_response(app, scope)
        assert status == 401
        assert json.loads(body)["error"] == "Unauthorized"

    @pytest.mark.asyncio
    async def test_invalid_token(self, app, monkeypatch):
        async def mock_resolve(token):
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        scope = _http_scope(headers=[(b"authorization", b"Bearer bad_token")])
        status, _, body = await _capture_response(app, scope)
        assert status == 401

    @pytest.mark.asyncio
    async def test_passes_through_non_http(self, app):
        """Non-HTTP scopes (lifespan) pass through without auth."""
        called = False

        async def inner(scope, receive, send):
            nonlocal called
            called = True

        middleware = MCPAuthMiddleware(inner)
        scope = {"type": "lifespan"}
        await middleware(scope, None, None)
        assert called


# ---------------------------------------------------------------------------
# MCPTokenPathAuth tests
# ---------------------------------------------------------------------------


class TestMCPTokenPathAuth:
    @pytest.fixture
    def app(self):
        return MCPTokenPathAuth(_echo_app)

    @pytest.mark.asyncio
    async def test_valid_key_in_path(self, app, monkeypatch):
        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        scope = _http_scope(path=f"/{_VALID_KEY}")
        status, _, body = await _capture_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/"
        assert data["principal_type"] == "api_key"

    @pytest.mark.asyncio
    async def test_path_rewrite_preserves_subpath(self, app, monkeypatch):
        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        scope = _http_scope(path=f"/{_VALID_KEY}/mcp")
        status, _, body = await _capture_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/mcp"

    @pytest.mark.asyncio
    async def test_missing_key(self, app):
        scope = _http_scope(path="/")
        status, _, body = await _capture_response(app, scope)
        assert status == 401
        assert "API key" in json.loads(body)["error"]

    @pytest.mark.asyncio
    async def test_non_api_key_prefix(self, app):
        scope = _http_scope(path="/bad_prefix_key")
        status, _, body = await _capture_response(app, scope)
        assert status == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key(self, app, monkeypatch):
        async def mock_resolve(token):
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        scope = _http_scope(path="/hc_nonexistent")
        status, _, body = await _capture_response(app, scope)
        assert status == 401

    @pytest.mark.asyncio
    async def test_passes_through_non_http(self, app):
        called = False

        async def inner(scope, receive, send):
            nonlocal called
            called = True

        middleware = MCPTokenPathAuth(inner)
        scope = {"type": "lifespan"}
        await middleware(scope, None, None)
        assert called

    @pytest.mark.asyncio
    async def test_principal_cleaned_up_after_request(self, app, monkeypatch):
        """_mcp_principal should be reset after the request completes."""

        async def mock_resolve(token):
            if token == _VALID_KEY:
                return _ADMIN_PRINCIPAL
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server._resolve_principal", mock_resolve)

        assert _mcp_principal.get() is None
        scope = _http_scope(path=f"/{_VALID_KEY}")
        await _capture_response(app, scope)
        assert _mcp_principal.get() is None


# ---------------------------------------------------------------------------
# OAuth token resolution tests
# ---------------------------------------------------------------------------


class TestMCPOAuthAuth:
    @pytest.mark.asyncio
    async def test_oauth_token_resolves_principal(self, monkeypatch):
        """OAuth access tokens should resolve to a user principal."""
        user_id = uuid.uuid4()

        async def mock_validate(db, token):
            if token == "oauth_test_token":
                return (user_id, "admin")
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server.validate_oauth_access_token", mock_validate)
        monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", AsyncMock)

        principal = await _resolve_principal("oauth_test_token")
        assert principal is not None
        assert principal.type == "oauth"
        assert principal.id == user_id
        assert principal.role == "admin"

    @pytest.mark.asyncio
    async def test_oauth_token_invalid_falls_through(self, monkeypatch):
        """Invalid OAuth tokens should return None."""

        async def mock_validate(db, token):
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server.validate_oauth_access_token", mock_validate)
        monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", AsyncMock)

        principal = await _resolve_principal("not_a_valid_token")
        assert principal is None
