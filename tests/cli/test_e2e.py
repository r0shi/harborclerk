"""End-to-end smoke tests: CLI main() → McpHttpClient → MCPAuthMiddleware → response.

Strategy (Option B — in-process integration):

  Instead of booting a live server on a real port, we wire the CLI's synchronous
  ``McpHttpClient`` directly to the real ``MCPAuthMiddleware`` ASGI code via a
  lightweight sync transport adapter.  This exercises the full chain:

    CLI main() → CliConfig → McpHttpClient → HTTP request
    → _AsgiSyncTransport → MCPAuthMiddleware (real code)
    → 403 / pass-through → McpHttpClient parses response
    → McpClientError / result → main() maps to exit code

  The only stubs are:
  - ``_resolve_principal``: returns a pre-built Principal (skips DB key lookup)
  - ``async_session_factory``: prevents the audit-log write from touching a DB
  - The MCP inner app (happy-path test): a minimal stub returning a valid
    JSON-RPC envelope so we don't need a real MCP session or DB for tool calls.

  This is the same mock pattern used by ``tests/api/test_cli_access_gate.py``
  (the ASGI-level gate tests), extended one layer to include the CLI process.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import MCPAuthMiddleware

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_KEY = "hc_test1234567890abcdef"
_ADMIN_PRINCIPAL = Principal(type="api_key", id=uuid.uuid4(), role="admin")


# ---------------------------------------------------------------------------
# Sync transport adapter: run an async ASGI app inside httpx.Client
# ---------------------------------------------------------------------------


class _AsgiSyncTransport(httpx.BaseTransport):
    """Synchronous httpx transport that dispatches requests to an ASGI app.

    ``asyncio.run()`` is used to drive the async ASGI coroutine from the
    synchronous ``McpHttpClient``.  This only works when there is no running
    event loop in the calling thread (which is always the case for the CLI's
    synchronous code path).
    """

    def __init__(self, app) -> None:
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return asyncio.run(self._async_handle(request))

    async def _async_handle(self, request: httpx.Request) -> httpx.Response:
        scope = {
            "type": "http",
            "method": request.method,
            "path": request.url.path,
            "query_string": (request.url.query or "").encode(),
            # ASGI spec requires header names to be lowercase bytes.
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
        }
        body = request.content
        messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": body}

        async def send(message):
            messages.append(message)

        await self._app(scope, receive, send)

        start = next(m for m in messages if m["type"] == "http.response.start")
        status: int = start["status"]
        raw_headers: list[tuple[bytes, bytes]] = start.get("headers", [])
        response_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        headers = httpx.Headers([(k.decode(), v.decode()) for k, v in raw_headers])
        return httpx.Response(status, headers=headers, content=response_body)


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


async def _mock_resolve_principal(token: str) -> Principal | None:
    """Return the admin principal for the test key; None otherwise."""
    if token == _VALID_KEY:
        return _ADMIN_PRINCIPAL
    return None


@asynccontextmanager
async def _mock_session_factory():
    """No-op async session factory — prevents any real DB calls."""
    session = AsyncMock()
    session.commit = AsyncMock()
    yield session


# ---------------------------------------------------------------------------
# Inner app stubs
# ---------------------------------------------------------------------------


async def _jsonrpc_ok_app(scope, receive, send):
    """Minimal inner ASGI app that returns a valid JSON-RPC tools/call response.

    Returns a ``kb_system_health``-style payload so the CLI can render it.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "stub",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "healthy",
                                "checks": {"postgres": "ok", "storage": "ok"},
                            }
                        ),
                    }
                ]
            },
        }
    ).encode()
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_client_transport(asgi_app):
    """Context-manager factory: patch McpHttpClient to use _AsgiSyncTransport.

    Replaces the ``_http`` attribute on the *instance* right after __init__
    runs, so all of the real init logic (config, headers, verify flag) still
    executes — only the underlying transport is swapped.
    """
    transport = _AsgiSyncTransport(asgi_app)
    original_init = __import__("harbor_clerk.cli.client", fromlist=["McpHttpClient"]).McpHttpClient.__init__

    def patched_init(self_inner, config):
        original_init(self_inner, config)
        # Replace the transport on the already-constructed httpx.Client
        self_inner._http = httpx.Client(
            transport=transport,
            base_url=config.url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=not config.insecure,
            headers=self_inner._http.headers,
        )

    return patch("harbor_clerk.cli.client.McpHttpClient.__init__", patched_init)


# ---------------------------------------------------------------------------
# Test: gate denial (ENABLE_CLI_ACCESS=false) → exit 3
# ---------------------------------------------------------------------------


def test_e2e_gate_denial_exits_3(monkeypatch):
    """Full chain: CLI → real MCPAuthMiddleware (gate=disabled) → exit 3.

    The middleware reads the harbor-clerk-cli/* User-Agent and the
    ENABLE_CLI_ACCESS setting, returns a real 403 JSON body, and the CLI maps
    that to exit code 3 (cli_disabled).
    """
    from harbor_clerk import config as _config
    from harbor_clerk.cli import main as cli_main

    # Gate disabled
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
    _config._settings = None  # force settings re-read

    # Set required CLI env vars
    monkeypatch.setenv("HARBOR_CLERK_URL", "http://testserver")
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", _VALID_KEY)

    # Build the gated ASGI app (gate wraps an echo inner app — never reached)
    gated_app = MCPAuthMiddleware(_jsonrpc_ok_app)

    with (
        patch("harbor_clerk.mcp_server._resolve_principal", _mock_resolve_principal),
        patch("harbor_clerk.mcp_server.async_session_factory", _mock_session_factory),
        patch("harbor_clerk.api.request_log.log_api_request", AsyncMock()),
        _patch_client_transport(gated_app),
    ):
        rc = cli_main.main(["system-health", "--json"])

    assert rc == 3, f"Expected exit 3 (cli_disabled), got {rc}"


# ---------------------------------------------------------------------------
# Test: happy path (ENABLE_CLI_ACCESS=true) → exit 0
# ---------------------------------------------------------------------------


def test_e2e_happy_path_exits_0(monkeypatch, capsys):
    """Full chain: CLI → real MCPAuthMiddleware (gate=enabled) → stub tool → exit 0.

    The middleware passes the request through to the stub inner app which
    returns a valid JSON-RPC envelope.  The CLI renders the result and exits 0.
    """
    from harbor_clerk import config as _config
    from harbor_clerk.cli import main as cli_main

    # Gate enabled
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
    _config._settings = None

    monkeypatch.setenv("HARBOR_CLERK_URL", "http://testserver")
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", _VALID_KEY)

    gated_app = MCPAuthMiddleware(_jsonrpc_ok_app)

    with (
        patch("harbor_clerk.mcp_server._resolve_principal", _mock_resolve_principal),
        _patch_client_transport(gated_app),
    ):
        rc = cli_main.main(["system-health", "--json"])

    assert rc == 0, f"Expected exit 0, got {rc}"

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data.get("status") == "healthy"
