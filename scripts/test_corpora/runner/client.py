"""Harbor Clerk REST client used by the test sweep harness.

Targets the actual Harbor Clerk API surface. SSE streaming for the Ask flow
uses ``httpx``'s SSE-by-line iteration. Selected methods retry transient
failures via ``tenacity`` with exponential backoff.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


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

    def wait_for_research(self, conv_id: str, max_wait_seconds: int) -> dict[str, Any]:
        """Poll until state is done/error or deadline. Sleep 5s between polls."""
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            res = self.poll_research(conv_id)
            if res.get("state") in {"done", "error"}:
                return res
            time.sleep(5)
        return {"state": "timeout", "conv_id": conv_id}

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
        """POST /api/watch/folders."""
        r = self._client.post("/api/watch/folders", json={"path": path, "display_name": name})
        r.raise_for_status()
        return r.json()
