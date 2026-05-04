import json

import httpx
import pytest

from scripts.test_corpora.runner.client import HarborClerkClient


def make_client(handler) -> HarborClerkClient:
    transport = httpx.MockTransport(handler)
    return HarborClerkClient(base_url="https://localhost", transport=transport, verify=False)


def test_start_research_returns_task_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/research/start"
        body = json.loads(request.content)
        assert body["question"] == "What?"
        assert body["model_id"] == "qwen3-8b"
        return httpx.Response(200, json={"task_id": "task-123"})

    c = make_client(handler)
    assert c.start_research("What?", model_id="qwen3-8b", depth="standard", time_limit=1800) == "task-123"


def test_poll_research_done():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "task_id": "task-123",
            "state": "done",
            "answer": "Answer text.",
            "citations": [{"doc_id": "doc-a"}, {"doc_id": "doc-b"}],
        })

    c = make_client(handler)
    res = c.poll_research("task-123")
    assert res["state"] == "done"
    assert [c["doc_id"] for c in res["citations"]] == ["doc-a", "doc-b"]


def test_pipeline_status_quiet():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}},
        })

    c = make_client(handler)
    assert c.pipeline_quiet() is True


def test_pipeline_status_busy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "queues": {"io": {"queued": 5, "running": 1}, "cpu": {"queued": 0, "running": 0}},
        })

    c = make_client(handler)
    assert c.pipeline_quiet() is False


def test_wipe_db_calls_maintenance_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"ok": True})

    c = make_client(handler)
    c.wipe_db(confirm=True)
    assert "POST /api/system/maintenance/wipe" in seen
