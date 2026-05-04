"""Harbor Clerk REST client used by the test sweep harness.

Targets the same surface the SPA uses. SSE streaming for the Ask flow uses
``httpx``'s SSE-by-line iteration. All methods retry transient failures
via ``tenacity`` with exponential backoff.
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
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            transport=transport,
            verify=verify,
            timeout=timeout_seconds,
        )

    # ── research ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def start_research(self, question: str, model_id: str, depth: str, time_limit: int) -> str:
        r = self._client.post(
            "/api/research/start",
            json={
                "question": question,
                "model_id": model_id,
                "depth": depth,
                "time_limit_seconds": time_limit,
            },
        )
        r.raise_for_status()
        return r.json()["task_id"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
    def poll_research(self, task_id: str) -> dict[str, Any]:
        r = self._client.get(f"/api/research/{task_id}")
        r.raise_for_status()
        return r.json()

    def wait_for_research(self, task_id: str, max_wait_seconds: int) -> dict[str, Any]:
        """Poll until done/error or deadline. Sleep 5s between polls."""
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            res = self.poll_research(task_id)
            if res.get("state") in {"done", "error"}:
                return res
            time.sleep(5)
        return {"state": "timeout", "task_id": task_id}

    # ── ask (chat SSE) ──

    def stream_ask(self, question: str, model_id: str) -> Iterator[dict]:
        """Yield SSE event dicts. Returns when ``done`` event received."""
        with self._client.stream(
            "POST",
            "/api/chat",
            json={"message": question, "model_id": model_id, "stream": True},
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
        r = self._client.get("/api/stats/queue")
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
        r = self._client.get("/api/system/health")
        r.raise_for_status()
        return r.json()

    # ── maintenance ──

    def wipe_db(self, confirm: bool = False) -> None:
        if not confirm:
            raise RuntimeError("wipe_db requires confirm=True")
        r = self._client.post("/api/system/maintenance/wipe", json={"confirm": True})
        r.raise_for_status()

    def watch_folder_add(self, path: str, name: str | None = None) -> dict[str, Any]:
        r = self._client.post("/api/watch/folders", json={"path": path, "display_name": name})
        r.raise_for_status()
        return r.json()
