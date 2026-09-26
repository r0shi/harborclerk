"""The chat answer's time budget (#719).

A chat answer was bounded by context and by tool rounds but not by time: a 26B model paged through a mailbox for 71
minutes and 19 model calls before the client gave up. ``settings.chat_time_budget_seconds`` bounds it. The clock is
faked, the model is a mock ``httpx.AsyncClient`` whose responses drive the loop, and the database is the test engine.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harbor_clerk.config import get_settings


def _chunk(**delta) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


def _tool_call_chunk(name="search_documents", arguments='{"query": "x"}') -> str:
    return _chunk(
        tool_calls=[
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}
        ]
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _mock_client(responses: list[list[str]], on_send=None):
    """An httpx.AsyncClient whose successive sends serve the given SSE line lists. ``on_send(n)`` runs before the
    n-th response streams, so a test can move the clock at a chosen point."""
    calls = {"n": 0}

    def build(*_a, **_k):
        n = calls["n"]
        calls["n"] += 1
        lines = responses[min(n, len(responses) - 1)]
        resp = MagicMock()
        resp.status_code = 200

        async def aiter_lines():
            if on_send:
                on_send(n)
            for line in lines:
                yield line

        resp.aiter_lines = aiter_lines
        resp.aclose = AsyncMock()
        resp.aread = AsyncMock()
        return resp

    client = MagicMock()
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(side_effect=build)
    client.aclose = AsyncMock()
    # The forced final answer uses ``client.stream(...)`` as an async context manager.
    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(side_effect=lambda *a, **k: build())
    stream_ctx.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_ctx)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, calls


@pytest.fixture
async def chat_session_factory(db_session, _engine, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as session:
            yield session

    monkeypatch.setattr("harbor_clerk.llm.chat.async_session_factory", _factory)
    monkeypatch.setattr("harbor_clerk.topics.async_session_factory", _factory)


async def _conversation(db_session, admin_user):
    from harbor_clerk.models.conversation import Conversation

    conv = Conversation(user_id=admin_user.user_id, title="Bounded chat", scope=None)
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


async def _drive(conv_id, admin_user, client, clock, monkeypatch, budget: float, on_tool=None):
    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", budget)
    monkeypatch.setattr(chat_module, "_now", clock)
    tool_calls: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        tool_calls.append(fn_name)
        if on_tool:
            on_tool()
        return json.dumps({"results": [], "total": 0})

    events = []
    with (
        patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=client),
    ):
        async for raw in chat_module.chat_stream(conv_id, "What did the Q3 contract cost?", admin_user.user_id):
            if raw.startswith("data: "):
                events.append(json.loads(raw[6:]))
    text = "".join(e.get("content", "") for e in events if e.get("type") == "token")
    done = next(e for e in events if e.get("type") == "done")
    return text, done, tool_calls


@pytest.mark.asyncio
async def test_a_budget_spent_between_rounds_forces_the_answer_and_says_so(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """Round one is a tool call that finishes inside the budget; by round two the budget is gone. No second
    search is made: the model is asked for its answer, the answer says it stopped early, and the done event
    names the reason."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="It cost $4,500 a month."), "data: [DONE]"]],
    )

    def budget_runs_out():  # the tool result comes back and the budget is gone
        clock.now = 301.0

    text, done, tool_calls = await _drive(
        conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0, on_tool=budget_runs_out
    )

    assert tool_calls == ["search_documents"], "the round inside the budget ran"
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("It cost $4,500 a month.")
    assert "I stopped searching after 5 minutes" in text
    assert client.stream.call_count == 1, "one forced final answer, without tools"


@pytest.mark.asyncio
async def test_a_budget_spent_mid_generation_cuts_the_stream_and_drops_the_partial_tool_call(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """One generation can outlast the budget. The stream is cut, the half-written tool call is not executed,
    and the answer is forced from what there is."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def advance(n):
        if n == 0:  # the deadline passes while the first response is still streaming
            clock.now = 301.0

    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="I found nothing yet."), "data: [DONE]"]],
        on_send=advance,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)

    assert tool_calls == [], "the tool call that was being written is dropped"
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("I found nothing yet.") and "stopped searching" in text


@pytest.mark.asyncio
async def test_an_answer_inside_the_budget_carries_no_stop_reason_and_no_note(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="Done."), "data: [DONE]"]])
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=300.0)
    assert tool_calls == ["search_documents"]
    assert "stop_reason" not in done
    assert text == "Done."


@pytest.mark.asyncio
async def test_a_zero_budget_means_no_bound(db_session, admin_user, chat_session_factory, monkeypatch):
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    clock.now = 10_000.0
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="Done."), "data: [DONE]"]])
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=0.0)
    assert tool_calls == ["search_documents"] and text == "Done." and "stop_reason" not in done
