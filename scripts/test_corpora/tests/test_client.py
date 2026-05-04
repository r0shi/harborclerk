import json

import httpx
import pytest

from scripts.test_corpora.runner.client import HarborClerkClient


def make_client(handler) -> HarborClerkClient:
    transport = httpx.MockTransport(handler)
    return HarborClerkClient(base_url="https://localhost", transport=transport, verify=False)


def test_login_stores_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            body = json.loads(request.content)
            assert body == {"email": "a@b.c", "password": "pw"}
            return httpx.Response(200, json={"access_token": "tok-xyz", "token_type": "bearer", "user": {"email": "a@b.c"}})
        # Subsequent call should carry the bearer
        assert request.headers.get("Authorization") == "Bearer tok-xyz"
        return httpx.Response(200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}})

    c = make_client(handler)
    c.login("a@b.c", "pw")
    assert c.pipeline_quiet() is True


def test_activate_model_calls_put():
    seen: list[tuple[str, str]] = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    c = make_client(handler)
    c.activate_model("qwen3-8b")
    assert ("PUT", "/api/chat/models/qwen3-8b/activate") in seen


def test_wait_for_model_ready_returns_true_when_ready():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        # First call: loading. Second call: ready.
        state = "loading" if calls["n"] == 1 else "ready"
        return httpx.Response(200, json={"state": state, "model_id": "qwen3-8b"})

    c = make_client(handler)
    assert c.wait_for_model_ready("qwen3-8b", max_wait_seconds=5, poll_seconds=0) is True


def test_wait_for_model_ready_returns_false_on_wrong_model():
    def handler(request):
        return httpx.Response(200, json={"state": "ready", "model_id": "qwen3-4b"})

    c = make_client(handler)
    # Wants qwen3-8b but the active is qwen3-4b — should time out and return False
    assert c.wait_for_model_ready("qwen3-8b", max_wait_seconds=1, poll_seconds=0) is False


def test_start_research_extracts_x_research_id_header():
    def handler(request):
        assert request.url.path == "/api/research"
        body = json.loads(request.content)
        assert body["question"] == "What?"
        assert body["depth"] == "standard"
        assert body["time_limit_minutes"] == 30
        # Return SSE stream with the conv_id in headers
        return httpx.Response(
            200,
            headers={"X-Research-Id": "conv-abc", "Content-Type": "text/event-stream"},
            content=b"data: {\"type\":\"started\"}\n\n",
        )

    c = make_client(handler)
    conv_id = c.start_research("What?", depth="standard", time_limit_minutes=30)
    assert conv_id == "conv-abc"


def test_poll_research_returns_done():
    def handler(request):
        assert request.url.path == "/api/research/conv-abc"
        return httpx.Response(200, json={"state": "done", "answer": "A", "citations": []})

    c = make_client(handler)
    res = c.poll_research("conv-abc")
    assert res["state"] == "done"


def test_pipeline_status_quiet():
    def handler(request):
        assert request.url.path == "/api/jobs/snapshot"
        return httpx.Response(200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}})

    c = make_client(handler)
    assert c.pipeline_quiet() is True


def test_pipeline_status_busy():
    def handler(request):
        return httpx.Response(200, json={"queues": {"io": {"queued": 5, "running": 1}, "cpu": {"queued": 0, "running": 0}}})

    c = make_client(handler)
    assert c.pipeline_quiet() is False


def test_delete_all_documents_requires_confirm_flag():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    c = make_client(handler)
    with pytest.raises(RuntimeError):
        c.delete_all_documents(confirm=False)


def test_delete_all_documents_sends_literal_confirmation():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"status": "ok"})

    c = make_client(handler)
    c.delete_all_documents(confirm=True)
    assert seen[0] == {"confirmation": "DELETE EVERYTHING"}


def test_create_conversation_returns_id():
    def handler(request):
        assert request.url.path == "/api/chat/conversations"
        return httpx.Response(200, json={"conversation_id": "conv-xyz", "mode": "chat"})

    c = make_client(handler)
    assert c.create_conversation(mode="chat") == "conv-xyz"
