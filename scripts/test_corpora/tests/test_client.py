import json

import httpx
import pytest

from scripts.test_corpora.runner.client import HarborClerkClient, _evaluate_health, _WatchdogState


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


def test_jwt_refresh_on_401_relogins_and_retries():
    """When a request returns 401 with an expired token, the client must
    transparently re-login and retry. Without this, long-running phase 4
    cells crash when HC's 30-minute access token TTL elapses mid-research."""
    bearers_seen: list[str | None] = []
    login_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            login_count["n"] += 1
            return httpx.Response(200, json={"access_token": "tok-fresh", "token_type": "bearer"})

        if request.url.path == "/api/jobs/snapshot":
            bearer = request.headers.get("Authorization")
            bearers_seen.append(bearer)
            # The "stale" pre-seeded token returns 401; the freshly-issued
            # token returns 200. This is exactly what HC does when the JWT
            # passes its TTL.
            if bearer == "Bearer tok-stale":
                return httpx.Response(401, json={"detail": "Token expired"})
            return httpx.Response(
                200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
            )
        return httpx.Response(404)

    c = make_client(handler)
    c.login("a@b.c", "pw")
    # Pre-seed a stale token to simulate "logged in 30 min ago, now expired"
    # without having to wait through tokens lifecycle.
    c._client.auth._token = "tok-stale"
    assert c.pipeline_quiet() is True

    # Exactly one login: the refresh after 401. (Pre-seed bypasses lazy login.)
    assert login_count["n"] == 1
    # The first attempt sent the stale token; the retry must carry the fresh one.
    assert bearers_seen == ["Bearer tok-stale", "Bearer tok-fresh"]


def test_jwt_refresh_does_not_loop_when_login_itself_401s():
    """If credentials are wrong, login() returns 401. We must NOT retry the
    login forever — the /api/auth/login path is skipped in auth_flow's
    401-retry. The original request's 401 still surfaces."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(401, json={"detail": "Invalid credentials"})
        return httpx.Response(401, json={"detail": "Token expired"})

    c = make_client(handler)
    c.login("wrong@b.c", "pw")
    # Use model_status (no tenacity retry wrapper) so we see the underlying
    # HTTPStatusError directly without it being wrapped in RetryError.
    with pytest.raises(httpx.HTTPStatusError):
        c.model_status()


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
        # First call: loading. Subsequent: ready.
        state = "loading" if calls["n"] == 1 else "ready"
        return httpx.Response(200, json={"state": state, "model_id": "qwen3-8b"})

    c = make_client(handler)
    assert c.wait_for_model_ready("qwen3-8b", max_wait_seconds=5, poll_seconds=0, consecutive_ready_required=3) is True


def test_wait_for_model_ready_requires_consecutive_ready_polls():
    """Single 'ready' flicker between 'loading' readings must NOT satisfy
    the wait. HC reports ready as soon as llama-server's /health returns 200,
    but llama-server can briefly answer 200 before the model is generation-
    ready. We require 3 consecutive ready readings to ride that out."""
    states = iter(["loading", "ready", "loading", "ready", "ready", "ready"])

    def handler(request):
        return httpx.Response(200, json={"state": next(states), "model_id": "qwen3-8b"})

    c = make_client(handler)
    assert c.wait_for_model_ready("qwen3-8b", max_wait_seconds=10, poll_seconds=0, consecutive_ready_required=3) is True


def test_wait_for_model_ready_returns_false_on_wrong_model():
    def handler(request):
        return httpx.Response(200, json={"state": "ready", "model_id": "qwen3-4b"})

    c = make_client(handler)
    # Wants qwen3-8b but the active is qwen3-4b — should time out and return False
    assert c.wait_for_model_ready("qwen3-8b", max_wait_seconds=1, poll_seconds=0, consecutive_ready_required=3) is False


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
        return httpx.Response(200, json={"conversation_id": "conv-xyz"})

    c = make_client(handler)
    assert c.create_conversation(title="my-test") == "conv-xyz"


def test_create_conversation_sends_only_title_no_null_no_mode():
    """Regression for the 422 bug: HC's CreateConversationRequest is
    ``title: str`` (no None default, no ``mode`` field). The harness used
    to send ``{"title": null, "mode": "chat"}`` which Pydantic rejects.
    Body must be exactly ``{"title": "<string>"}``."""
    seen_bodies: list[dict] = []

    def handler(request):
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"conversation_id": "conv-xyz"})

    c = make_client(handler)
    c.create_conversation(title="cuad-ask-1")
    assert seen_bodies == [{"title": "cuad-ask-1"}]


def test_create_conversation_default_title_is_string():
    """When no title is passed, the harness must still send a non-null
    string — never ``{"title": null}``."""
    seen_bodies: list[dict] = []

    def handler(request):
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"conversation_id": "conv-xyz"})

    c = make_client(handler)
    c.create_conversation()  # no title arg
    assert isinstance(seen_bodies[0].get("title"), str)
    assert seen_bodies[0]["title"]  # non-empty


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


def test_run_research_drains_sse_stream_until_done():
    """Regression test for the interrupted-on-disconnect bug. The harness
    MUST hold the SSE stream open until the server emits ``done``;
    closing early triggers HC's research_stream finally-block which
    marks the task ``interrupted`` and yields empty answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/research":
            # Simulate an SSE response with several events ending in 'done'
            body = (
                'data: {"type":"started"}\n\n'
                'data: {"type":"token","content":"hello "}\n\n'
                'data: {"type":"token","content":"world"}\n\n'
                'data: {"type":"done","conversation_id":"conv-abc"}\n\n'
            )
            return httpx.Response(
                200,
                headers={"X-Research-Id": "conv-abc", "Content-Type": "text/event-stream"},
                content=body,
            )
        # Final poll for ResearchDetail
        if request.method == "GET" and request.url.path == "/api/research/conv-abc":
            return httpx.Response(
                200,
                json={"status": "completed", "report": "hello world", "conversation_id": "conv-abc"},
            )
        return httpx.Response(404)

    c = make_client(handler)
    conv_id, result = c.run_research("Q?", depth="standard", time_limit_minutes=1)
    assert conv_id == "conv-abc"
    assert result["status"] == "completed"
    assert result["report"] == "hello world"


def test_evaluate_health_no_abort_when_model_ready_and_research_running():
    state = _WatchdogState()
    kind, detail = _evaluate_health("ready", "running", state)
    assert kind is None and detail is None
    assert state.model_bad == 0
    assert state.research_bad == 0


def test_evaluate_health_aborts_after_two_consecutive_model_bad():
    """Two consecutive non-ready model readings must trigger model_unhealthy.
    This is the case we're trying to catch: llama-server crashes, HC's
    /api/chat/models/status flips from 'ready' to 'loading'/'errored'/etc.,
    and the harness needs to bail rather than wait for HC's read timeout."""
    state = _WatchdogState()
    kind, _ = _evaluate_health("loading", None, state)
    assert kind is None  # one bad reading isn't enough
    assert state.model_bad == 1
    kind, detail = _evaluate_health("loading", None, state)
    assert kind == "model_unhealthy"
    assert "loading" in (detail or "")


def test_evaluate_health_treats_status_call_failure_as_bad():
    """If GET /api/chat/models/status itself raises, the watchdog passes
    None for model_state. Two such failures count as bad — HC API death
    is just as much a stopper as model death."""
    state = _WatchdogState()
    _evaluate_health(None, None, state)
    kind, _ = _evaluate_health(None, None, state)
    assert kind == "model_unhealthy"


def test_evaluate_health_resets_on_recovery():
    """A transient blip ('loading' once, then 'ready') must NOT trigger
    a bail — otherwise model warm-up races would kill every research."""
    state = _WatchdogState()
    _evaluate_health("loading", None, state)
    assert state.model_bad == 1
    kind, _ = _evaluate_health("ready", None, state)
    assert kind is None
    assert state.model_bad == 0


def test_evaluate_health_aborts_after_two_consecutive_research_failed():
    """If HC has marked the research interrupted/failed but somehow the SSE
    stream is still open, the watchdog should bail. Same threshold so a
    transient poll error doesn't masquerade as a state transition."""
    state = _WatchdogState()
    _evaluate_health("ready", "interrupted", state)
    kind, detail = _evaluate_health("ready", "interrupted", state)
    assert kind == "research_failed"
    assert "interrupted" in (detail or "")


def test_evaluate_health_research_bad_resets_on_running():
    """Research status flipping to 'running' between bad readings resets
    the counter — only a sustained bad state triggers a bail."""
    state = _WatchdogState()
    _evaluate_health("ready", "interrupted", state)
    assert state.research_bad == 1
    _evaluate_health("ready", "running", state)
    assert state.research_bad == 0


def test_run_research_returns_interrupted_when_server_interrupts():
    """If HC reports the task as interrupted (e.g. real disconnect for
    some other reason, or model-switch race), run_research must surface
    that status faithfully so the caller can downgrade the unit."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/research":
            body = 'data: {"type":"error","message":"llm error"}\n\n'
            return httpx.Response(
                200,
                headers={"X-Research-Id": "conv-int", "Content-Type": "text/event-stream"},
                content=body,
            )
        if request.method == "GET" and request.url.path == "/api/research/conv-int":
            return httpx.Response(
                200,
                json={"status": "interrupted", "report": None, "error": "llm error"},
            )
        return httpx.Response(404)

    c = make_client(handler)
    conv_id, result = c.run_research("Q?", depth="standard", time_limit_minutes=1)
    assert result["status"] == "interrupted"
    assert result.get("report") is None
