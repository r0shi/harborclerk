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
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

# Watchdog tunables for run_research. Two consecutive bad readings (~60s by
# default) force the SSE stream closed so we don't sit on a hung llama-server.
WATCHDOG_INTERVAL_SECONDS = 30
WATCHDOG_THRESHOLD = 2


@dataclass
class _WatchdogState:
    model_bad: int = 0
    research_bad: int = 0


def _evaluate_health(
    model_state: str | None,
    research_status: str | None,
    state: _WatchdogState,
    threshold: int = WATCHDOG_THRESHOLD,
) -> tuple[str | None, str | None]:
    """Update consecutive-bad counters and decide whether to abort an in-flight research.

    Returns ``(abort_kind, abort_detail)`` — both ``None`` when no abort.

    A "bad" model reading is anything other than ``state == "ready"`` (including
    ``model_state is None``, which means the status call itself failed). A "bad"
    research reading is one of ``{interrupted, failed, error}``; anything else,
    including ``None`` (no conv_id yet, or transient poll failure), resets that
    counter. We require ``threshold`` consecutive bad readings before bailing —
    one transient blip shouldn't kill a long-running research.
    """
    if model_state == "ready":
        state.model_bad = 0
    else:
        state.model_bad += 1
    if state.model_bad >= threshold:
        return ("model_unhealthy", f"model state={model_state}")

    if research_status in ("interrupted", "failed", "error"):
        state.research_bad += 1
    else:
        state.research_bad = 0
    if state.research_bad >= threshold:
        return ("research_failed", f"research status={research_status}")

    return (None, None)


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
        from mcp.client.streamable_http import streamablehttp_client

        async with (
            streamablehttp_client(
                self._url,
                headers=self._headers,
                timeout=self._timeout,
            ) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            return result.tools

    async def _call_tool_async(self, name: str, args: dict) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with (
            streamablehttp_client(
                self._url,
                headers=self._headers,
                timeout=self._timeout,
            ) as (read, write, _),
            ClientSession(read, write) as session,
        ):
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

    def active_research(self) -> dict[str, Any]:
        """GET /api/research/active — returns ``{conversation_id, status}`` or
        a falsy ``conversation_id`` when nothing is running."""
        r = self._client.get("/api/research/active")
        r.raise_for_status()
        return r.json()

    def delete_research(self, conv_id: str) -> None:
        """DELETE /api/research/{conv_id}. Used to clean up orphaned
        research tasks left behind when a previous sweep was killed
        mid-Phase-4."""
        r = self._client.delete(f"/api/research/{conv_id}")
        r.raise_for_status()

    def cleanup_orphan_research(self) -> str | None:
        """If a research task is currently active, delete it. Returns the
        conv_id that was deleted (or None). Idempotent."""
        active = self.active_research()
        conv_id = active.get("conversation_id")
        if conv_id:
            self.delete_research(conv_id)
        return conv_id

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def poll_research(self, conv_id: str) -> dict[str, Any]:
        """GET /api/research/{conv_id} — returns the JSON body."""
        r = self._client.get(f"/api/research/{conv_id}")
        r.raise_for_status()
        return r.json()

    def run_research(
        self,
        question: str,
        depth: str = "standard",
        time_limit_minutes: int = 30,
        strategy: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """POST /api/research and drain the SSE stream until the research
        finishes server-side. Returns ``(conv_id, ResearchDetail)``.

        Harbor Clerk's research handler runs as an SSE generator; if the
        client disconnects mid-stream, the handler's finally block marks
        the task ``interrupted``. So we MUST hold the connection open
        for the duration. Trying to "fire and poll" by closing the
        stream early — which an earlier version of this client did —
        ends every research at round 0 with ``status="interrupted"``.

        A watchdog thread polls model + research status every
        ``WATCHDOG_INTERVAL_SECONDS``. Two consecutive bad readings
        (model not ``ready``, or research already ``interrupted``/``failed``)
        forces the SSE stream closed so we don't sit on a hung llama-server.
        When that happens the returned dict carries ``harness_aborted=True``
        plus ``harness_abort_reason``.

        This call is NOT retried by ``tenacity`` because partial SSE
        streams can't be safely re-opened against the same conv_id.
        """
        body: dict[str, Any] = {
            "question": question,
            "depth": depth,
            "time_limit_minutes": time_limit_minutes,
        }
        if strategy is not None:
            body["strategy"] = strategy

        # Read timeout must accommodate the full time_limit + slack for
        # synthesis tail.
        read_timeout = time_limit_minutes * 60 + 180
        timeout = httpx.Timeout(connect=30, read=read_timeout, write=10, pool=10)

        abort_event = threading.Event()
        abort_reason: dict[str, str] = {}
        conv_id_holder: list[str | None] = [None]
        response_holder: list[Any] = [None]

        def _close_stream() -> None:
            r = response_holder[0]
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass

        def _watchdog() -> None:
            wstate = _WatchdogState()
            while not abort_event.wait(WATCHDOG_INTERVAL_SECONDS):
                try:
                    ms_state = self.model_status().get("state")
                except Exception:
                    ms_state = None
                rs_status: str | None = None
                cid = conv_id_holder[0]
                if cid:
                    try:
                        rs_status = self.poll_research(cid).get("status")
                    except Exception:
                        rs_status = None
                kind, detail = _evaluate_health(ms_state, rs_status, wstate)
                if kind is not None:
                    abort_reason["kind"] = kind
                    abort_reason["detail"] = detail or kind
                    _close_stream()
                    return

        watchdog: threading.Thread | None = None
        with self._client.stream("POST", "/api/research", json=body, timeout=timeout) as r:
            r.raise_for_status()
            conv_id = r.headers.get("X-Research-Id")
            if not conv_id:
                raise RuntimeError("research start did not return X-Research-Id")
            conv_id_holder[0] = conv_id
            response_holder[0] = r

            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.start()

            try:
                for line in r.iter_lines():
                    if abort_event.is_set():
                        break
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if not payload.strip():
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype in ("done", "error"):
                        break
            except Exception:
                # Watchdog forced the stream closed → expected. Anything else
                # is a real error worth surfacing.
                if not abort_reason:
                    raise
            finally:
                abort_event.set()
                if watchdog is not None:
                    watchdog.join(timeout=5)

        # Fetch the final state (status, report, citations, messages).
        final = self.poll_research(conv_id)
        if abort_reason:
            final = {
                **final,
                "harness_aborted": True,
                "harness_abort_reason": abort_reason.get("detail", "unknown"),
            }
        return conv_id, final

    def wait_for_research(self, conv_id: str, max_wait_seconds: int, poll_seconds: int = 5) -> dict[str, Any]:
        """[Deprecated] Poll until status is completed/failed/interrupted or deadline.

        Kept for backward-compat in tests but no longer used by the sweep —
        ``run_research`` now blocks on the SSE stream itself, which is the
        only safe way (closing the stream early triggers HC's
        interrupted-on-disconnect handler).

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

    def wait_for_pipeline_activity(self, max_wait_seconds: int = 120, poll_seconds: int = 2) -> bool:
        """Wait until the queue is *non*-empty (something has been enqueued).

        Used right after ``watch_folder_add`` to close the race where the
        watcher hasn't scanned the folder yet — without this, a quick
        ``wait_for_quiet_pipeline`` poll could see an empty queue, declare
        the corpus ingested, and start research on zero documents.
        """
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            if not self.pipeline_quiet():
                return True
            if poll_seconds > 0:
                time.sleep(poll_seconds)
        return False

    def document_count(self) -> int:
        """GET /api/docs?limit=0 — total active document count.

        Used to verify ingestion landed the expected number of documents.
        Note: counts everything HC's watcher decided to ingest, which may
        include synthetic-corpus JSON sidecars unless HC's watcher filters
        them.
        """
        r = self._client.get("/api/docs", params={"limit": 0})
        r.raise_for_status()
        return r.json().get("total", 0)

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
