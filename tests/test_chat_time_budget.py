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


def _mock_client(responses: list[list[str]], on_send=None, on_line=None):
    """An httpx.AsyncClient whose successive sends serve the given SSE line lists. ``on_send(n)`` runs before the
    n-th response streams and ``on_line(n, i)`` after its i-th line, so a test can move the clock at a chosen
    point."""
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
            for i, line in enumerate(lines):
                yield line
                if on_line:
                    on_line(n, i)

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
    assert "I stopped after 5 minutes" in text
    assert client.send.call_count == 1, "no second round was started once the budget was gone"
    assert client.stream.call_count == 1, "one forced final answer, without tools"


@pytest.mark.asyncio
async def test_the_forced_answer_is_cut_after_its_grace(db_session, admin_user, chat_session_factory, monkeypatch):
    """The forced final answer is bounded too: past the grace it keeps what has streamed and stops."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [_chunk(content="First. "), _chunk(content="Second. "), _chunk(content="Third."), "data: [DONE]"],
        ],
        on_line=lambda n, i: setattr(clock, "now", 301.0 + 121.0) if (n == 1 and i == 0) else None,
    )
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("First. ") and "Second" not in text and "I stopped after" in text


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
    assert text.startswith("I found nothing yet.") and "I stopped after" in text


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


@pytest.mark.asyncio
async def test_an_answer_being_written_at_the_deadline_gets_the_grace_and_then_stands(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model finished searching and is writing its answer when the budget runs out. The answer is not cut
    at the deadline (that would cut a correct answer mid-sentence for being slow); it gets the same grace a
    forced answer would, and what streamed by then stands, with the note. No forced final call is made."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def move(n, i):
        if n == 1 and i == 0:
            clock.now = 301.0  # past the deadline, inside the grace: keep writing
        if n == 1 and i == 1:
            clock.now = 301.0 + 121.0  # past the grace: cut

    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [_chunk(content="The fee "), _chunk(content="was $4,500. "), _chunk(content="Per month."), "data: [DONE]"],
        ],
        on_line=move,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == ["search_documents"]
    assert text.startswith("The fee was $4,500. ") and "Per month" not in text
    assert "I stopped after" in text and done["stop_reason"] == "time_budget"
    assert client.stream.call_count == 0, "what the user watched arrive is the answer; nothing is forced"


@pytest.mark.asyncio
async def test_text_streamed_beside_a_dropped_tool_call_stays_in_front_of_the_forced_answer(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The user watched a preamble arrive, then a tool call started and the budget ran out. The call is
    dropped; the saved message is what the user saw, then the forced answer, so reload shows what was watched."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    partial_call = _chunk(
        tool_calls=[
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_documents", "arguments": '{"que'},
            }
        ]
    )
    client, _ = _mock_client(
        [
            [
                _chunk(content="Let me look. "),
                partial_call,
                _chunk(tool_calls=[{"index": 0, "function": {"arguments": 'ry": "x"}'}}]),
                "data: [DONE]",
            ],
            [_chunk(content="Nothing found."), "data: [DONE]"],
        ],
        on_line=lambda n, i: setattr(clock, "now", 301.0) if (n == 0 and i == 1) else None,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == [], "the half-written call is not executed"
    assert text.startswith("Let me look. Nothing found.")
    assert done["stop_reason"] == "time_budget"

    from sqlalchemy import select

    from harbor_clerk.models.chat_message import ChatMessage

    rows = (
        (await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id)))
        .scalars()
        .all()
    )
    saved = [r.content for r in rows if r.role == "assistant" and r.content]
    assert saved and saved[-1].startswith("Let me look. Nothing found."), "the stored message is the watched one"


@pytest.mark.asyncio
async def test_the_tool_round_cap_is_reported_as_a_stop_reason(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    rounds = chat_module._MAX_TOOL_ROUNDS
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"]] * rounds + [[_chunk(content="Capped."), "data: [DONE]"]]
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert len(tool_calls) == rounds
    assert done["stop_reason"] == "tool_rounds" and text == "Capped."


@pytest.mark.asyncio
async def test_the_context_budget_is_reported_as_a_stop_reason(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    monkeypatch.setattr(chat_module, "_TOOL_LOOP_BUDGET", 0.0)  # any usage at all exhausts it after round one
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="From what I have."), "data: [DONE]"]]
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert tool_calls == ["search_documents"]
    assert done["stop_reason"] == "context_budget" and text == "From what I have."
    assert "I stopped after" not in text, "the note is the time budget's"


@pytest.mark.asyncio
async def test_a_consumer_that_stops_during_a_keepalive_leaves_no_pending_fetch(monkeypatch):
    """The cut lands between items: the fetch of the next line is cancelled with the iterator, not left to
    fail unawaited when the response closes."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "_KEEPALIVE_INTERVAL", 0.01)
    released = asyncio.Event()

    async def silent_model():
        await released.wait()
        yield "data: never"

    fetches_before = {t for t in asyncio.all_tasks() if not t.done()}
    it = chat_module._iter_with_keepalive(silent_model())
    first = await it.__anext__()
    assert first is chat_module._KEEPALIVE_SENTINEL
    await it.aclose()
    await asyncio.sleep(0)
    pending = {t for t in asyncio.all_tasks() if not t.done()} - fetches_before
    assert pending == set(), f"a fetch was left pending: {pending}"


@pytest.mark.asyncio
async def test_a_long_tool_call_or_thinking_generation_still_sends_the_client_keepalives(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """#731: the model streams tool-call arguments (or its thinking) for minutes; the client hears none of it,
    and a keepalive keyed to the model's silence never fires. One is sent once the client has heard nothing
    for the keepalive interval."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    arg_delta = lambda piece: _chunk(tool_calls=[{"index": 0, "function": {"arguments": piece}}])  # noqa: E731
    first_round = [
        _chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_documents", "arguments": ""},
                }
            ]
        ),
        arg_delta('{"query": '),
        arg_delta('"a long '),
        arg_delta('argument"}'),
        "data: [DONE]",
    ]
    # Each argument delta arrives 31 s after the last: the model is never silent, the client always is.
    client, _ = _mock_client(
        [first_round, [_chunk(content="Done."), "data: [DONE]"]],
        on_line=lambda n, i: setattr(clock, "now", clock.now + 31.0) if n == 0 else None,
    )
    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", 0.0)
    monkeypatch.setattr(chat_module, "_now", clock)

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        return json.dumps({"results": [], "total": 0})

    raw: list[str] = []
    with (
        patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=client),
    ):
        async for line in chat_module.chat_stream(conv.conversation_id, "q?", admin_user.user_id):
            raw.append(line)
    keepalives = [i for i, line in enumerate(raw) if line.startswith(": keepalive")]
    first_token = next(i for i, line in enumerate(raw) if '"type": "token"' in line)
    assert len(keepalives) >= 3, f"one keepalive per silent half-minute; got {len(keepalives)}"
    assert keepalives[0] < first_token, "the keepalives come while the tool call is being written"


@pytest.mark.asyncio
async def test_the_watched_prefix_is_stored_even_when_the_forced_call_yields_nothing(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The forced call can produce nothing (a 500, a timeout, a cut before its first token). The stored message
    still begins with the text the user watched arrive, then the note."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    partial_call = _chunk(
        tool_calls=[
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_documents", "arguments": '{"que'},
            }
        ]
    )
    client, _ = _mock_client(
        [[_chunk(content="Let me look. "), partial_call, "data: [DONE]"], []],
        on_line=lambda n, i: setattr(clock, "now", 301.0) if (n == 0 and i == 1) else None,
    )
    # The forced call fails outright.
    failed = MagicMock()
    failed.status_code = 500
    failed.aclose = AsyncMock()
    client.stream.return_value.__aenter__ = AsyncMock(return_value=failed)
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == [] and done["stop_reason"] == "time_budget"

    from sqlalchemy import select

    from harbor_clerk.models.chat_message import ChatMessage

    rows = (
        (await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id)))
        .scalars()
        .all()
    )
    saved = [r.content for r in rows if r.role == "assistant" and r.content]
    assert saved and saved[-1].startswith("Let me look. ") and "I stopped after" in saved[-1]
    assert text.startswith("Let me look. "), "and it is what the client received"


@pytest.mark.asyncio
async def test_an_answer_that_completes_late_is_not_tagged_as_cut(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model wrote its whole answer; only its "[DONE]" arrives after the grace. Nothing was cut, so there is
    no note and no stop reason."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [[_chunk(content="All of it. "), _chunk(content="Every word."), "data: [DONE]"]],
        on_line=lambda n, i: setattr(clock, "now", 1000.0) if (n == 0 and i == 1) else None,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert text == "All of it. Every word." and "stop_reason" not in done


@pytest.mark.asyncio
async def test_a_cut_during_a_keepalive_leaves_no_pending_fetch_on_the_production_path(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The budget runs out while the model is silent: the loop leaves with `break` during a keepalive. The
    fetch of the next line must be cancelled then, not left to fail unawaited when the response closes."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "_KEEPALIVE_INTERVAL", 0.01)
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    never = asyncio.Event()

    async def silent_after_one_token():
        yield _chunk(content="Start ")
        clock.now = 301.0 + 121.0  # the deadline and the grace pass while the model is silent
        await never.wait()
        yield "data: never"

    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_lines = silent_after_one_token
    resp.aclose = AsyncMock()
    client = MagicMock()
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    before = {t for t in asyncio.all_tasks() if not t.done()}
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    await asyncio.sleep(0)
    pending = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert pending == set(), f"a fetch was left pending: {pending}"
    assert text.startswith("Start ") and done["stop_reason"] == "time_budget"
