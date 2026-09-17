"""A small client for driving a live Harbor Clerk instance.

The eval harness has a fuller one (`scripts/test_corpora/runner/client.py`)
with retries and research helpers, but it lives in a separate uv project with
its own dependencies, so this suite carries what it needs on `httpx` and `mcp`,
both root dependencies. Methods return parsed JSON and raise on unexpected
status. `request()` returns the raw response and is the only way to reach an
endpoint without a method here; the checks use it for status-code assertions,
and the session helpers use it for two mutating calls: key deletion at
teardown, and the guarded `delete-all-documents` of wipe mode.

A user session refreshes its access token once on a 401 and retries: the
token lives 30 minutes by default and a run with OCR can outlast that, and
the teardown that removes the folder must not be the call that fails.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

FOREGROUND_STAGES = ("extract", "chunk", "embed", "finalize")


def folder_ingest_done(progress: dict[str, Any], expected_files: int) -> bool:
    """The folder has been scanned, every expected file has finalized, and no
    foreground stage has work left. `summarize` is background, does not gate
    finalize, and blocks indefinitely with no model, so it is not counted."""
    foreground_in_flight = sum(
        counts["pending"] + counts["running"] for stage, counts in progress["by_stage"].items() if stage != "summarize"
    )
    return (
        progress["scan_status"] == "idle"
        and foreground_in_flight == 0
        and progress["completed_files"] >= expected_files
    )


def folder_stage_errors(progress: dict[str, Any]) -> dict[str, int]:
    """Foreground stages with errored jobs; a non-empty result means waiting
    longer cannot help, so the wait fails now instead of at the timeout."""
    return {
        stage: counts["error"]
        for stage, counts in progress["by_stage"].items()
        if stage != "summarize" and counts.get("error", 0) > 0
    }


def document_ready(doc: dict[str, Any]) -> bool:
    stages = {j["stage"]: j["status"] for j in doc.get("jobs", [])}
    return doc.get("pipeline_status") == "ready" and all(stages.get(s) == "done" for s in FOREGROUND_STAGES)


class HarborClerk:
    def __init__(
        self,
        base_url: str,
        *,
        verify: bool = True,
        timeout: float = 60.0,
        bearer: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        self._http = httpx.Client(
            base_url=self.base_url,
            verify=verify,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers=headers,
            follow_redirects=False,
            transport=transport,
        )
        self._verify = verify
        self._timeout = timeout
        self._transport = transport
        self._session_user = False  # True after login(): a refresh cookie exists
        self.user: dict[str, Any] | None = None
        self.refreshes = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HarborClerk:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── raw ─────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """No status check. For assertions about 401/403/422 and the like.
        A logged-in user session refreshes once on 401 and retries."""
        r = self._http.request(method, path, **kw)
        if r.status_code == 401 and self._session_user and not path.startswith("/api/auth/"):
            try:
                self.refresh()
            except httpx.HTTPStatusError:
                return r
            r = self._http.request(method, path, **kw)
        return r

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = self.request(method, path, **kw)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{method} {path} -> {r.status_code}: {r.text[:500]}", request=r.request, response=r
            )
        return r.json() if r.content else None

    # ── auth ────────────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict[str, Any]:
        data = self._json("POST", "/api/auth/login", json={"email": email, "password": password})
        self._http.headers["Authorization"] = f"Bearer {data['access_token']}"
        self.user = data.get("user")
        self._session_user = True
        return data

    def refresh(self) -> dict[str, Any]:
        """Uses the httponly refresh cookie the login set; rotates the token."""
        data = self._json("POST", "/api/auth/refresh")
        self._http.headers["Authorization"] = f"Bearer {data['access_token']}"
        self.refreshes += 1
        return data

    def me(self) -> dict[str, Any]:
        return self._json("GET", "/api/me")

    def cookie(self, name: str) -> str | None:
        return self._http.cookies.get(name)

    def with_key(self, raw_key: str) -> HarborClerk:
        """A separate client authenticated as an API key, same target."""
        return HarborClerk(
            self.base_url, verify=self._verify, timeout=self._timeout, bearer=raw_key, transport=self._transport
        )

    # ── system ──────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/api/system/health")

    def setup_status(self) -> dict[str, Any]:
        return self._json("GET", "/api/system/setup-status")

    def status_summary(self) -> dict[str, Any]:
        return self._json("GET", "/api/system/status-summary")

    def watch_system(self) -> dict[str, Any]:
        """`platform` is "docker" on Compose, where WATCH_ROOT confines folders."""
        return self._json("GET", "/api/watch/system")

    def document_count(self) -> int:
        # limit=0 means "no limit" on this route and would page the whole corpus.
        return int(self._json("GET", "/api/docs", params={"limit": 1}).get("total", 0))

    # ── watched folders ─────────────────────────────────────────────────

    def folder_create(self, path: str, *, recursive: bool = True) -> dict[str, Any]:
        return self._json("POST", "/api/watch/folders", json={"path": path, "recursive": recursive})

    def folder_list(self) -> list[dict[str, Any]]:
        return self._json("GET", "/api/watch/folders")

    def folder_progress(self, folder_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/watch/folders/{folder_id}/progress")

    def folder_delete(self, folder_id: str) -> None:
        self._json("DELETE", f"/api/watch/folders/{folder_id}")

    def wait_for_folder_ingest(
        self, folder_id: str, *, expected_files: int, timeout_s: float, poll_s: float = 3.0
    ) -> dict[str, Any]:
        """Folder-scoped: judged from the folder's own progress, so other work
        on the instance neither delays nor fails the run. Raises TimeoutError
        with the last progress payload, which is what a human needs."""
        deadline = time.monotonic() + timeout_s
        progress: dict[str, Any] = {}
        while time.monotonic() < deadline:
            progress = self.folder_progress(folder_id)
            errors = folder_stage_errors(progress)
            if errors:
                raise RuntimeError(f"folder {folder_id} has errored stages, waiting cannot help: {errors}")
            if folder_ingest_done(progress, expected_files):
                return progress
            time.sleep(poll_s)
        raise TimeoutError(f"folder {folder_id} did not finish ingesting in {timeout_s:.0f}s: {json.dumps(progress)}")

    # ── search ──────────────────────────────────────────────────────────

    def search(self, query: str, **body: Any) -> dict[str, Any]:
        return self._json("POST", "/api/search", json={"query": query, **body})

    def find_all(self, query: str, **body: Any) -> dict[str, Any]:
        return self._json("POST", "/api/search/find-all", json={"query": query, **body})

    def read_passages(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/api/passages/read", json=body)

    # ── documents ───────────────────────────────────────────────────────

    def list_documents(self, **params: Any) -> dict[str, Any]:
        return self._json("GET", "/api/docs", params=params)

    def get_document(self, doc_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/docs/{doc_id}")

    def document_content(self, doc_id: str, **params: Any) -> dict[str, Any]:
        return self._json("GET", f"/api/docs/{doc_id}/content", params=params)

    def document_entities(self, doc_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/docs/{doc_id}/entities")

    def document_outline(self, doc_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/docs/{doc_id}/outline")

    def reprocess_document(self, doc_id: str) -> Any:
        return self._json("POST", f"/api/docs/{doc_id}/reprocess")

    def delete_document(self, doc_id: str) -> None:
        """Soft delete (admin). Used only on documents this run created."""
        self._json("DELETE", f"/api/docs/{doc_id}")

    def wait_for_document_ready(self, doc_id: str, *, timeout_s: float, poll_s: float = 3.0) -> dict[str, Any]:
        """Judged from the document's own stage jobs, not the instance queues."""
        deadline = time.monotonic() + timeout_s
        doc: dict[str, Any] = {}
        while time.monotonic() < deadline:
            doc = self.get_document(doc_id)
            if document_ready(doc):
                return doc
            if doc.get("pipeline_status") == "error":  # PipelineStatus.error; there is no "failed"
                raise RuntimeError(f"document {doc_id} errored: {doc.get('error')}")
            time.sleep(poll_s)
        raise TimeoutError(
            f"document {doc_id} not ready in {timeout_s:.0f}s: pipeline_status={doc.get('pipeline_status')}"
        )

    # ── API keys ────────────────────────────────────────────────────────

    def create_api_key(self, name: str, *, permission_tier: str = "full", **fields: Any) -> dict[str, Any]:
        return self._json("POST", "/api/api-keys", json={"name": name, "permission_tier": permission_tier, **fields})

    def delete_api_key(self, key_id: str) -> None:
        self._json("DELETE", f"/api/api-keys/{key_id}")

    def key_requests(self, key_id: str, **params: Any) -> dict[str, Any]:
        return self._json("GET", f"/api/api-keys/{key_id}/usage/requests", params=params)

    # ── models and Ask ──────────────────────────────────────────────────

    def list_models(self) -> list[dict[str, Any]]:
        data = self._json("GET", "/api/chat/models")
        return data if isinstance(data, list) else data.get("models", [])

    def model_status(self) -> dict[str, Any]:
        return self._json("GET", "/api/chat/models/status")

    def activate_model(self, model_id: str) -> Any:
        """Admin. Loads the model into llama-server; 404 if it is not downloaded."""
        return self._json("PUT", f"/api/chat/models/{model_id}/activate")

    def wait_for_model_ready(
        self, model_id: str, *, timeout_s: float, consecutive: int = 3, poll_s: float = 3.0
    ) -> None:
        """llama-server reports ready before it can serve, so require `consecutive` ready polls in a row."""
        deadline = time.monotonic() + timeout_s
        streak = 0
        while time.monotonic() < deadline:
            status = self.model_status()
            streak = streak + 1 if status.get("state") == "ready" and status.get("model_id") == model_id else 0
            if streak >= consecutive:
                return
            time.sleep(poll_s)
        raise TimeoutError(f"model {model_id} not ready in {timeout_s:.0f}s: {status}")

    def create_conversation(self, title: str, *, scope: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {"title": title}
        if scope:
            body["scope"] = scope
        return self._json("POST", "/api/chat/conversations", json=body)["conversation_id"]

    def delete_conversation(self, conv_id: str) -> None:
        self._json("DELETE", f"/api/chat/conversations/{conv_id}")

    def stream_ask(self, conv_id: str, content: str, *, timeout_s: float) -> list[dict[str, Any]]:
        """Send one message and drain the SSE stream; returns every event, the
        last of which is `done` with `rag_context.citations` when there were any.

        `timeout_s` is the whole answer's budget, not the gap between events: an
        agentic answer that keeps calling tools streams steadily and would
        otherwise run for as long as the model cares to (one did, for 19 min).
        Leaving the `with` block closes the connection, which is the only
        cancellation the endpoint offers."""
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_s
        with self._http.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/messages",
            json={"content": content},
            timeout=httpx.Timeout(connect=10, read=min(timeout_s, 120), write=10, pool=10),
        ) as r:
            if r.status_code >= 400:
                r.read()
                raise httpx.HTTPStatusError(f"ask -> {r.status_code}: {r.text[:300]}", request=r.request, response=r)
            for line in r.iter_lines():
                if time.monotonic() > deadline:
                    kinds = [e.get("type") for e in events[-5:]]
                    raise TimeoutError(f"ask exceeded {timeout_s:.0f}s after {len(events)} events; last: {kinds}")
                if not line.startswith("data: ") or not line[6:].strip():
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if event.get("type") == "done":
                    break
        return events


class McpSession:
    """Synchronous façade over `mcp.ClientSession` on streamable HTTP, one
    connection per call, after the eval harness's `SyncMcpSession`."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60.0):
        self.url = url
        self._headers = headers or {}
        self._timeout = timeout

    @classmethod
    def bearer(cls, base_url: str, token: str, **kw: Any) -> McpSession:
        return cls(f"{base_url.rstrip('/')}/mcp/", headers={"Authorization": f"Bearer {token}"}, **kw)

    @classmethod
    def url_token(cls, base_url: str, raw_key: str, **kw: Any) -> McpSession:
        return cls(f"{base_url.rstrip('/')}/t/{raw_key}", **kw)

    def _connect(self):
        """mcp 1.28 renamed the transport and moved headers and timeout into an
        `httpx.AsyncClient`; older installs take them as keyword arguments."""
        from mcp.client import streamable_http as sh

        if hasattr(sh, "streamable_http_client"):
            http = httpx.AsyncClient(headers=self._headers, timeout=httpx.Timeout(self._timeout), follow_redirects=True)
            return sh.streamable_http_client(self.url, http_client=http)
        return sh.streamablehttp_client(self.url, headers=self._headers, timeout=self._timeout)

    async def _with_session(self, fn):
        from mcp import ClientSession

        async with self._connect() as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)

    def list_tool_names(self) -> list[str]:
        result = asyncio.run(self._with_session(lambda s: s.list_tools()))
        return sorted(t.name for t in result.tools)

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """The raw `CallToolResult`; see `tool_json` / `tool_error`."""
        return asyncio.run(self._with_session(lambda s: s.call_tool(name, args)))


def tool_text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def tool_json(result: Any) -> Any:
    """Harbor Clerk tools return one JSON text block; parse it or explain why not."""
    text = tool_text(result)
    if getattr(result, "isError", False):
        raise AssertionError(f"tool call returned an error: {text[:500]}")
    return json.loads(text)


def tool_error(result: Any) -> str | None:
    """The error text if the tool call failed, else None."""
    return tool_text(result) if getattr(result, "isError", False) else None
