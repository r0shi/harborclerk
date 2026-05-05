"""Harbor Clerk REST client used by the test sweep harness.

Targets the actual Harbor Clerk API surface. SSE streaming for the Ask flow
uses ``httpx``'s SSE-by-line iteration. Selected methods retry transient
failures via ``tenacity`` with exponential backoff.

Also provides :class:`SyncMcpSession` — a thin synchronous wrapper around the
async ``mcp`` SDK, used by the Phase 1 baseline generator.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class SyncMcpSession:
    """Synchronous façade over ``mcp.ClientSession`` + streamable-HTTP transport.

    Opens the MCP connection once via :meth:`connect`, exposes ``list_tools()``
    and ``call_tool()`` as synchronous calls (each wraps a fresh ``asyncio.run``),
    and tears the session down via :meth:`close`.

    Use as a context manager::

        with SyncMcpSession(url="https://localhost/mcp", headers={...}) as sess:
            tools = sess.list_tools()
            result = sess.call_tool("kb_search", {"query": "..."})

    Design note: ``asyncio.run()`` per call is slightly wasteful (each call
    creates/destroys an event loop) but is safe and avoids the complexity of
    a background thread running a persistent event loop. For Phase 1 baselines
    — which are latency-insensitive — this is the simplest correct approach.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout

    # ── async internals ──

    async def _list_tools_async(self) -> list[Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(
            self._url,
            headers=self._headers,
            timeout=self._timeout,
        ) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools

    async def _call_tool_async(self, name: str, args: dict) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(
            self._url,
            headers=self._headers,
            timeout=self._timeout,
        ) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)

    # ── public synchronous API ──

    def list_tools(self) -> list[Any]:
        """Return a list of ``mcp.types.Tool`` objects."""
        return asyncio.run(self._list_tools_async())

    def call_tool(self, name: str, args: dict) -> Any:
        """Execute one MCP tool call and return the ``CallToolResult``."""
        return asyncio.run(self._call_tool_async(name, args))

    # ── context manager ──

    def __enter__(self) -> SyncMcpSession:
        return self

    def __exit__(self, *_: Any) -> None:
        pass  # each call manages its own connection lifecycle


class HarborClerkClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        timeout_seconds: float = 30.0,
    ):
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            transport=transport,
            verify=verify,
            timeout=timeout_seconds,
        )

    # ── auth ──

    def login(self, email: str, password: str) -> None:
        """POST /api/auth/login — stores the access token as a Bearer header."""
        r = self._client.post("/api/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        token = r.json()["access_token"]
        self._client.headers["Authorization"] = f"Bearer {token}"

    # ── model management ──

    def activate_model(self, model_id: str) -> None:
        """PUT /api/chat/models/{model_id}/activate — async, model loads in the background."""
        r = self._client.put(f"/api/chat/models/{model_id}/activate")
        r.raise_for_status()

    def deactivate_model(self) -> None:
        """PUT /api/chat/models/deactivate — stop the active model."""
        r = self._client.put("/api/chat/models/deactivate")
        r.raise_for_status()

    def model_status(self) -> dict[str, Any]:
        """GET /api/chat/models/status — returns {state, model_id}."""
        r = self._client.get("/api/chat/models/status")
        r.raise_for_status()
        return r.json()

    def wait_for_model_ready(
        self,
        model_id: str,
        max_wait_seconds: int = 600,
        poll_seconds: int = 2,
    ) -> bool:
        """Poll model_status() until state=='ready' and model_id matches.

        Returns True on success, False on timeout or persistent mismatch.
        When poll_seconds=0 (test mode) the sleep is skipped.
        """
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            status = self.model_status()
            if status.get("state") == "ready" and status.get("model_id") == model_id:
                return True
            if poll_seconds > 0:
                time.sleep(poll_seconds)
        return False

    # ── research ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def start_research(
        self,
        question: str,
        depth: str = "standard",
        time_limit_minutes: int = 30,
        strategy: str | None = None,
    ) -> str:
        """POST /api/research — returns the X-Research-Id header as conv_id.

        The endpoint returns SSE. We open the stream, read the header, then
        close immediately without draining the body. The server runs the
        research regardless; we poll via GET to get the result.
        """
        body: dict[str, Any] = {
            "question": question,
            "depth": depth,
            "time_limit_minutes": time_limit_minutes,
        }
        if strategy is not None:
            body["strategy"] = strategy

        with self._client.stream("POST", "/api/research", json=body) as r:
            r.raise_for_status()
            conv_id = r.headers.get("X-Research-Id")
            if not conv_id:
                raise RuntimeError("research start did not return X-Research-Id")
            return conv_id

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def poll_research(self, conv_id: str) -> dict[str, Any]:
        """GET /api/research/{conv_id} — returns the JSON body."""
        r = self._client.get(f"/api/research/{conv_id}")
        r.raise_for_status()
        return r.json()

    def wait_for_research(
        self, conv_id: str, max_wait_seconds: int, poll_seconds: int = 5
    ) -> dict[str, Any]:
        """Poll until status is completed/failed/interrupted or deadline.

        Harbor Clerk returns a ``ResearchDetail`` object whose terminal values
        are ``completed``, ``failed``, and ``interrupted`` (not ``done`` / ``error``).
        When poll_seconds=0 the sleep is skipped (test mode).
        """
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            res = self.poll_research(conv_id)
            if res.get("status") in {"completed", "failed", "interrupted"}:
                return res
            if poll_seconds > 0:
                time.sleep(poll_seconds)
        return {"status": "timeout", "conv_id": conv_id}

    # ── ask (chat SSE) ──

    def create_conversation(self, title: str | None = None, mode: str = "chat") -> str:
        """POST /api/chat/conversations — returns conversation_id."""
        r = self._client.post(
            "/api/chat/conversations",
            json={"title": title, "mode": mode},
        )
        r.raise_for_status()
        return r.json()["conversation_id"]

    def stream_ask(self, conv_id: str, content: str) -> Iterator[dict]:
        """POST /api/chat/conversations/{conv_id}/messages — drain SSE, yield each event dict.

        NOT retried — a partial stream is not safely retryable.
        """
        with self._client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/messages",
            json={"content": content},
            timeout=httpx.Timeout(connect=10, read=600, write=10, pool=10),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if not payload.strip():
                    continue
                event = json.loads(payload)
                yield event
                if event.get("type") == "done":
                    return

    # ── pipeline / system ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def pipeline_status(self) -> dict[str, Any]:
        """GET /api/jobs/snapshot."""
        r = self._client.get("/api/jobs/snapshot")
        r.raise_for_status()
        return r.json()

    def pipeline_quiet(self) -> bool:
        s = self.pipeline_status()
        q = s["queues"]
        return all(q[name]["queued"] == 0 and q[name]["running"] == 0 for name in q)

    def wait_for_quiet_pipeline(self, max_wait_seconds: int = 7200, poll_seconds: int = 30) -> bool:
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            if self.pipeline_quiet():
                return True
            time.sleep(poll_seconds)
        return False

    def health(self) -> dict[str, Any]:
        """GET /api/system/health."""
        r = self._client.get("/api/system/health")
        r.raise_for_status()
        return r.json()

    # ── maintenance ──

    def delete_all_documents(self, confirm: bool = False) -> None:
        """POST /api/system/delete-all-documents with the required literal confirmation string."""
        if not confirm:
            raise RuntimeError("delete_all_documents requires confirm=True")
        r = self._client.post(
            "/api/system/delete-all-documents",
            json={"confirmation": "DELETE EVERYTHING"},
        )
        r.raise_for_status()

    def watch_folder_add(self, path: str, name: str | None = None) -> dict[str, Any]:
        """POST /api/watch/folders.

        ``name`` is accepted for caller convenience but the Watch API schema only
        has ``path`` and ``recursive``; ``display_name`` is silently ignored by
        the server.
        """
        r = self._client.post("/api/watch/folders", json={"path": path})
        r.raise_for_status()
        return r.json()

    def watch_folder_list(self) -> list[dict]:
        """GET /api/watch/folders — returns a list of registered watch-folder objects."""
        r = self._client.get("/api/watch/folders")
        r.raise_for_status()
        return r.json()

    def watch_folder_delete(self, folder_id: str) -> None:
        """DELETE /api/watch/folders/{folder_id}."""
        r = self._client.delete(f"/api/watch/folders/{folder_id}")
        r.raise_for_status()
