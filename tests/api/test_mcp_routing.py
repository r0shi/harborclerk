"""Regression tests for the FastAPI mount routing for /mcp.

Bug history: PR #397 mounted the MCP Streamable HTTP app at /mcp, but the
SDK returns a Starlette *wrapper* around the actual handler — that wrapper
has its own /mcp route inside, so mounting it at /mcp meant requests had to
target /mcp/mcp (double prefix) to reach the handler.

Even after extracting the inner handler, a mount matches only below its
prefix, so the exact path /mcp fell to the SPA catch-all
(@app.get("/{full_path:path}"), GET-only → 405 for POST). The bare /mcp and
/t paths are now routed, every method, to the same apps as the mounts.

These tests pin: the mount reaches the middleware for /mcp/, the bare /mcp and
/t reach the same apps with the SPA served, and the middleware-handler chain
is the bare ASGI handler (not the Starlette wrapper).
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_post_mcp_trailing_slash_reaches_middleware(monkeypatch):
    """POST /mcp/ must reach MCPAuthMiddleware (not 404, not 405).

    The exact response status depends on the auth header (no key → 401),
    but the key assertion is that we get an MCP-shaped response, not the
    SPA catch-all's 405 `allow: GET`.
    """
    from harbor_clerk import mcp_server
    from harbor_clerk.api.app import create_app

    # Capture whether the middleware was reached. We don't care about the
    # downstream MCP behavior here — only that the request was routed past
    # FastAPI's mount and into the auth middleware.
    captured: dict = {}
    orig = mcp_server.MCPAuthMiddleware.__call__

    async def patched(self, scope, receive, send):
        captured["reached"] = True
        captured["scope_path"] = scope.get("path")
        captured["method"] = scope.get("method")
        # Return a 999 so we can identify this codepath specifically.
        await send({"type": "http.response.start", "status": 999, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(mcp_server.MCPAuthMiddleware, "__call__", patched)

    app = create_app()

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent_status: dict = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent_status["status"] = msg["status"]

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer hc_test")],
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
        "client": ("127.0.0.1", 0),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    assert captured.get("reached") is True, (
        f"POST /mcp/ did NOT reach MCPAuthMiddleware — got status {sent_status.get('status')}. "
        f"The SPA catch-all or another route is shadowing the mount."
    )
    assert sent_status["status"] == 999

    monkeypatch.setattr(mcp_server.MCPAuthMiddleware, "__call__", orig)


def test_create_mcp_app_returns_bare_handler_not_starlette_wrapper():
    """create_mcp_app must extract the bare StreamableHTTPASGIApp out of the
    Starlette wrapper returned by mcp.streamable_http_app(). Mounting the
    wrapper would create a double-prefix routing collision.
    """
    from harbor_clerk.mcp_server import create_mcp_app

    header_app, token_app, session_manager = create_mcp_app()

    # Both middleware wrappers should be wrapping the SAME bare handler —
    # if we accidentally went back to wrapping the Starlette wrapper, both
    # `header_app.app` and `token_app.app` would be Starlette instances.
    inner_header = header_app.app
    inner_token = token_app.app

    # The bare handler exposes `session_manager` as an attribute — Starlette
    # apps don't. This pins the contract that we unwrapped successfully.
    assert hasattr(inner_header, "session_manager"), (
        f"Header-auth middleware is wrapping a Starlette wrapper "
        f"({type(inner_header).__name__}) instead of the bare ASGI handler. "
        "Routing to /mcp/ won't work."
    )
    assert hasattr(inner_token, "session_manager"), (
        f"Token-path middleware is wrapping a Starlette wrapper "
        f"({type(inner_token).__name__}) instead of the bare ASGI handler."
    )
    assert inner_header is inner_token, "Both middlewares should wrap the same handler instance"
    assert session_manager is inner_header.session_manager


@pytest.fixture
def app_with_spa(tmp_path, monkeypatch):
    """The app with a static dir, so the SPA catch-all is registered as in production."""
    import harbor_clerk.config as _config
    from harbor_clerk.api.app import create_app

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>")
    monkeypatch.setenv("STATIC_DIR", str(static))
    _config._settings = None
    return create_app()


_INITIALIZE = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
    b'"capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
)


async def _post(app, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, bytes]:
    async def receive():
        return {"type": "http.request", "body": _INITIALIZE, "more_body": False}

    sent: dict = {"body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            *(headers or []),
        ],
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
        "client": ("127.0.0.1", 0),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return sent["status"], sent["body"]


@pytest.mark.parametrize(("bare", "slashed"), [("/mcp", "/mcp/"), ("/t", "/t/")])
async def test_bare_mcp_path_answers_initialize_like_the_slashed_one(app_with_spa, bare, slashed):
    """An unauthenticated initialize to /mcp (or /t) gets the MCP 401, as /mcp/ does: no 405, no redirect."""
    bare_status, bare_body = await _post(app_with_spa, bare)
    slashed_status, slashed_body = await _post(app_with_spa, slashed)
    assert slashed_status == 401
    assert (bare_status, bare_body) == (slashed_status, slashed_body)


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_bare_mcp_path_reaches_the_auth_middleware(app_with_spa, monkeypatch, path):
    """With a bearer token, POST /mcp is handed to MCPAuthMiddleware like /mcp/."""
    from harbor_clerk import mcp_server

    captured: dict = {}

    async def patched(self, scope, receive, send):
        captured["reached"] = True
        await send({"type": "http.response.start", "status": 999, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(mcp_server.MCPAuthMiddleware, "__call__", patched)
    status, _ = await _post(app_with_spa, path, [(b"authorization", b"Bearer hc_test")])
    assert captured.get("reached") is True
    assert status == 999
