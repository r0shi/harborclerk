"""MCP-over-HTTP JSON-RPC client for the harbor-clerk CLI."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from harbor_clerk.cli import __version__
from harbor_clerk.cli.config import CliConfig

ErrorKind = Literal["connection", "auth", "cli_disabled", "http", "protocol"]


@dataclass
class McpClientError(Exception):
    kind: ErrorKind
    message: str
    status_code: int | None = None
    body: Any = None

    def __str__(self) -> str:
        return self.message


class McpHttpClient:
    """Synchronous JSON-RPC over HTTP client for POST /mcp."""

    def __init__(self, config: CliConfig) -> None:
        self._config = config
        verify = not config.insecure
        self._http = httpx.Client(
            base_url=config.url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=verify,
            # Follow redirects: FastAPI's `redirect_slashes=True` default sends
            # POST /mcp -> 307 POST /mcp/. httpx defaults to follow_redirects=False
            # which would surface as a confusing 307 to the user.
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "User-Agent": f"harbor-clerk-cli/{__version__}",
                "Content-Type": "application/json",
                # Streamable HTTP MCP can stream tool responses as SSE; advertising
                # both content types lets the server pick whichever is best for the
                # call (the inner handler streams events for tools/call).
                "Accept": "application/json, text/event-stream",
            },
        )

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        request_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            # POST to /mcp/ (trailing slash): the server mounts the bare MCP
            # ASGI handler at /mcp, but FastAPI's mount semantics on the exact
            # path `/mcp` collide with the SPA catch-all route — sending to
            # `/mcp/` lands cleanly inside the mount.
            resp = self._http.post("/mcp/", json=body)
        except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as e:
            raise McpClientError(kind="connection", message=str(e)) from e

        if resp.status_code == 401:
            raise McpClientError(
                kind="auth",
                message="Authentication failed (HTTP 401). Check HARBOR_CLERK_API_KEY.",
                status_code=401,
                body=_safe_json(resp),
            )
        if resp.status_code == 403:
            payload = _safe_json(resp) or {}
            if isinstance(payload, dict) and payload.get("error") == "cli_access_disabled":
                raise McpClientError(
                    kind="cli_disabled",
                    message=payload.get(
                        "hint",
                        "CLI access disabled. Enable in System Settings → Integrations.",
                    ),
                    status_code=403,
                    body=payload,
                )
            raise McpClientError(
                kind="http",
                message=f"HTTP 403: {payload}",
                status_code=403,
                body=payload,
            )
        if resp.status_code >= 400:
            raise McpClientError(
                kind="http",
                message=f"HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )

        envelope = _parse_mcp_response(resp)
        if not isinstance(envelope, dict):
            raise McpClientError(kind="protocol", message="Non-JSON response body.")
        if "error" in envelope:
            raise McpClientError(
                kind="protocol",
                message=f"JSON-RPC error: {envelope['error']}",
                body=envelope,
            )

        result = envelope.get("result", {})
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text  # tool returned plain text
        return result

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> McpHttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _parse_mcp_response(resp) -> Any:
    """Parse the response body as MCP JSON-RPC.

    The MCP Streamable HTTP transport requires clients to advertise both
    `application/json` and `text/event-stream` in Accept; the server picks
    SSE for tool responses. We accept either: plain JSON or SSE-formatted
    `event: message\\ndata: <json>` frames.
    """
    content_type = resp.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        # Extract the first JSON-RPC payload from the SSE stream. MCP servers
        # send a single `event: message` frame per call with the JSON-RPC
        # response as the `data:` value. Other frames (heartbeats, progress)
        # are ignored — we return the first parseable one.
        for line in resp.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload = stripped[len("data:") :].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return None
    return _safe_json(resp)
