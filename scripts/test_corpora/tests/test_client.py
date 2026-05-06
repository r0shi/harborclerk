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
            return httpx.Response(
                200, json={"access_token": "tok-xyz", "token_type": "bearer", "user": {"email": "a@b.c"}}
            )
        # Subsequent call should carry the bearer
        assert request.headers.get("Authorization") == "Bearer tok-xyz"
        return httpx.Response(
            200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
        )

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
            content=b'data: {"type":"started"}\n\n',
        )

    c = make_client(handler)
    conv_id = c.start_research("What?", depth="standard", time_limit_minutes=30)
    assert conv_id == "conv-abc"


def test_poll_research_returns_completed():
    """poll_research should pass through whatever the server returns — the terminal
    value check lives in wait_for_research."""

    def handler(request):
        assert request.url.path == "/api/research/conv-abc"
        return httpx.Response(200, json={"status": "completed", "report": "A", "citations": []})

    c = make_client(handler)
    res = c.poll_research("conv-abc")
    assert res["status"] == "completed"


def test_wait_for_research_returns_on_completed():
    """wait_for_research should return as soon as status == 'completed'."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            # First poll: still running
            return httpx.Response(200, json={"status": "running", "report": None})
        # Second poll: done
        return httpx.Response(200, json={"status": "completed", "report": "Final answer"})

    c = make_client(handler)
    res = c.wait_for_research("conv-abc", max_wait_seconds=10, poll_seconds=0)
    assert res["status"] == "completed"
    assert res["report"] == "Final answer"
    assert calls["n"] == 2


def test_wait_for_research_timeout():
    """wait_for_research returns a timeout dict if deadline is hit without terminal status."""

    def handler(request):
        return httpx.Response(200, json={"status": "running"})

    c = make_client(handler)
    res = c.wait_for_research("conv-xyz", max_wait_seconds=0, poll_seconds=0)
    assert res["status"] == "timeout"
    assert res["conv_id"] == "conv-xyz"


def test_pipeline_status_quiet():
    def handler(request):
        assert request.url.path == "/api/jobs/snapshot"
        return httpx.Response(
            200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
        )

    c = make_client(handler)
    assert c.pipeline_quiet() is True


def test_pipeline_status_busy():
    def handler(request):
        return httpx.Response(
            200, json={"queues": {"io": {"queued": 5, "running": 1}, "cpu": {"queued": 0, "running": 0}}}
        )

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


def test_stream_ask_aggregates_token_events():
    """stream_ask should yield token events with type='token' and content=<text>."""
    sse_body = (
        b'data: {"type": "token", "content": "hello"}\n\n'
        b'data: {"type": "token", "content": " world"}\n\n'
        b'data: {"type": "done", "rag_context": {"citations": []}}\n\n'
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body,
        )

    c = make_client(handler)
    events = list(c.stream_ask("conv-abc", "What is X?"))
    tokens = [e for e in events if e.get("type") == "token"]
    final_text = "".join(e.get("content", "") for e in tokens)
    assert final_text == "hello world"
    assert any(e.get("type") == "done" for e in events)


def test_watch_folder_list_returns_list():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/watch/folders"
        return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/a"}])

    c = make_client(handler)
    folders = c.watch_folder_list()
    assert folders == [{"folder_id": "f1", "path": "/tmp/a"}]


def test_watch_folder_delete_calls_delete():
    seen: list[tuple[str, str]] = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    c = make_client(handler)
    c.watch_folder_delete("folder-42")
    assert ("DELETE", "/api/watch/folders/folder-42") in seen


def test_watch_folder_add_sends_path_only():
    """watch_folder_add should send only 'path' (display_name is ignored by server)."""
    seen_body: list[dict] = []

    def handler(request):
        seen_body.append(json.loads(request.content))
        return httpx.Response(200, json={"folder_id": "new-id"})

    c = make_client(handler)
    c.watch_folder_add("/some/path", name="my-folder")
    assert seen_body[0] == {"path": "/some/path"}


def test_sync_mcp_session_uses_correct_streamable_function():
    """Regression test for the streamable_http_client / streamablehttp_client
    confusion. The MCP SDK exports both names; only one accepts a `headers`
    kwarg, which we depend on for forwarding HC's bearer token. This test
    checks the exact symbol is imported AND that its signature accepts
    `headers`. Without it, Phase 1 baselines crash on the very first MCP
    list_tools() call after Phase 0 completes."""
    import inspect

    from mcp.client.streamable_http import streamablehttp_client

    sig = inspect.signature(streamablehttp_client)
    assert "headers" in sig.parameters, (
        f"MCP SDK's streamablehttp_client no longer accepts headers — signature is {sig}. "
        f"The harness needs that param to forward HC's bearer token to the MCP endpoint."
    )

    # Also assert SyncMcpSession imports the right name (catches future regressions
    # where someone swaps it back to the no-headers variant).
    import scripts.test_corpora.runner.client as client_module

    source = inspect.getsource(client_module)
    assert "from mcp.client.streamable_http import streamablehttp_client" in source, (
        "client.py should import streamablehttp_client (with headers support), "
        "not streamable_http_client (which doesn't take headers)."
    )


def test_active_research_returns_state():
    """Light coverage for the GET /api/research/active happy path."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/research/active"
        return httpx.Response(200, json={"conversation_id": "conv-orphan", "status": "running"})

    c = make_client(handler)
    res = c.active_research()
    assert res["conversation_id"] == "conv-orphan"


def test_cleanup_orphan_research_deletes_when_active():
    """If GET /research/active returns a conv_id, cleanup_orphan_research
    must DELETE it. This prevents the 409 Conflict on the very next
    start_research after a killed sweep."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/api/research/active":
            return httpx.Response(200, json={"conversation_id": "conv-orphan", "status": "running"})
        if request.method == "DELETE" and request.url.path == "/api/research/conv-orphan":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    c = make_client(handler)
    deleted = c.cleanup_orphan_research()
    assert deleted == "conv-orphan"
    assert "GET /api/research/active" in seen
    assert "DELETE /api/research/conv-orphan" in seen


def test_cleanup_orphan_research_no_op_when_idle():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"conversation_id": None, "status": None})

    c = make_client(handler)
    deleted = c.cleanup_orphan_research()
    assert deleted is None
    # Should have called GET but NOT DELETE
    assert any("GET" in s for s in seen)
    assert not any("DELETE" in s for s in seen)


def test_wait_for_pipeline_activity_returns_true_on_first_busy_poll():
    """When the queue is empty initially and becomes busy, the helper
    returns True. With poll_seconds=0 we hit the second response on the
    next iteration."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
            )
        return httpx.Response(
            200, json={"queues": {"io": {"queued": 7, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
        )

    c = make_client(handler)
    assert c.wait_for_pipeline_activity(max_wait_seconds=5, poll_seconds=0) is True


def test_wait_for_pipeline_activity_returns_false_on_timeout():
    """If the queue stays empty until the deadline, the helper returns False —
    the caller can decide whether that's a watcher misconfiguration."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
        )

    c = make_client(handler)
    # max_wait_seconds=0 → first poll only, no busy state ever → False
    assert c.wait_for_pipeline_activity(max_wait_seconds=0, poll_seconds=0) is False


def test_document_count_uses_total_field():
    """document_count returns the response body's `total` field, ignoring
    `items` (we pass limit=0 to avoid the actual list)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/docs"
        assert request.url.params.get("limit") == "0"
        return httpx.Response(200, json={"items": [], "total": 1234, "limit": 0, "offset": 0})

    c = make_client(handler)
    assert c.document_count() == 1234
