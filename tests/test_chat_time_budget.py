"""The chat answer's time budget (#719).

A chat answer was bounded by context and by tool rounds but not by time: a 26B model paged through a mailbox for 71
minutes and 19 model calls before the client gave up. ``settings.chat_time_budget_seconds`` bounds it. The clock is
faked, the model is a mock ``httpx.AsyncClient`` whose responses drive the loop, and the database is the test engine.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
    """The forced call can produce nothing (here a 500). The stored message still begins with the text the user
    watched arrive; the app's sentence for the failure follows it, and the note that the model "answered from what
    it had found" does not, since it did not."""
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
    failed.text = ""
    failed.aread = AsyncMock()
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
    assert saved and saved[-1].startswith("Let me look. \n\n_The model server failed while the final answer was being")
    assert "(HTTP 500)" in saved[-1] and "I stopped after" not in saved[-1]
    assert text == saved[-1], "and it is what the client received"


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
async def test_a_cut_during_a_keepalive_cancels_the_fetch_before_the_response_is_closed(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The budget runs out while the model is silent: the loop leaves with `break` during a keepalive. The
    fetch of the next line must already be cancelled when the response is closed, not left for the garbage
    collector to close later, racing the close and failing unawaited."""
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

    before = {t for t in asyncio.all_tasks() if not t.done()}
    live_at_close: list[set] = []

    async def close_response():
        # What the fetch task looks like at the moment the response is closed: cancelled already, or not.
        live_at_close.append({t for t in asyncio.all_tasks() if not t.done() and not t.cancelling()} - before)

    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_lines = silent_after_one_token
    resp.aclose = AsyncMock(side_effect=close_response)
    client = MagicMock()
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert live_at_close and live_at_close[0] == set(), (
        f"a fetch was still running when the response closed: {live_at_close}"
    )
    assert text.startswith("Start ") and done["stop_reason"] == "time_budget"


# --- a model server lost mid-stream (#690) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx.ReadError("Connection reset by peer"), id="read-reset"),
        pytest.param(
            httpx.RemoteProtocolError("peer closed connection without sending complete message body"), id="cut"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_reset_mid_stream_ends_the_chat_like_a_lost_server(
    db_session, admin_user, chat_session_factory, monkeypatch, exc
):
    """A swap or a crash while llama-server is writing resets the connection: httpx raises ``ReadError`` (or
    ``RemoteProtocolError``) from the line iterator, not ``ConnectError``. The generator must end the way it ends for
    a refused connection, with an error event, a stored message and a ``done``, rather than let the exception out and
    drop the stream."""
    from sqlalchemy import select

    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)

    def reset_after_first_token(n, i):
        raise exc

    client, _ = _mock_client([[_chunk(content="The Q3 contract"), "data: [DONE]"]], on_line=reset_after_first_token)
    _, done, _ = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert done["type"] == "done", "the generator finished instead of raising"
    stored = (
        (
            await db_session.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv.conversation_id, ChatMessage.role == "assistant"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1 and stored[0].content.startswith("Error: LLM server is not running")


@pytest.mark.asyncio
async def test_a_reset_during_the_forced_answer_keeps_what_was_streamed_and_finishes(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The forced final answer is a second stream from the same server. A reset while it is being written (the
    swap landed between the rounds and the answer) ends the answer where it was, as a refusal would, instead of
    escaping the generator: what was streamed is kept and the stream is closed with its done event."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def advance(n):
        if n == 0:
            clock.now = 301.0  # the deadline passes while the tool call streams: the answer will be forced

    def reset_the_forced_answer(n, i):
        if n == 1:
            raise httpx.ReadError("Connection reset by peer")

    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="I found"), "data: [DONE]"]],
        on_send=advance,
        on_line=reset_the_forced_answer,
    )
    text, done, _ = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert done["stop_reason"] == "time_budget", "the generator finished instead of raising"
    assert text.startswith("I found"), "what was streamed before the reset is kept"


# ── #742: the forced final answer, when the model thinks, and when it produces nothing ──


def _reasoning_chunk(text: str) -> str:
    """A thinking delta as llama-server streams it: ``reasoning_content``, not ``content``."""
    return _chunk(reasoning_content=text)


def _finish_chunk(reason: str) -> str:
    """llama-server's last chunk: an empty delta carrying ``finish_reason`` ("stop", or "length" when the context
    ran out under the model), before "[DONE]"."""
    return "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]})


def _forced_response(client, aiter_lines) -> None:
    """Serve the forced final call from a hand-written line generator (the mock's lists cannot go silent)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_lines = aiter_lines
    resp.aclose = AsyncMock()
    client.stream.return_value.__aenter__ = AsyncMock(return_value=resp)


async def _assistant_rows(db_session, conv_id) -> list[str]:
    from sqlalchemy import select

    from harbor_clerk.models.chat_message import ChatMessage

    rows = (await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id))).scalars()
    return [r.content for r in rows if r.role == "assistant" and r.content]


@pytest.mark.asyncio
async def test_the_forced_call_asks_for_no_thinking_and_says_the_search_is_over(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The forced final call carries no tools, the request fields that stop a model thinking first (llama-server's
    ``enable_thinking`` template kwarg; ``reasoning_effort`` for gpt-oss, which thinks regardless), and a last user
    message saying the search has ended. Without that message Qwen3.6 wrote its next tool call as text, eight
    times in ten. The message is the call's, not the conversation's: it is not stored."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="From the results, $4,500."), "data: [DONE]"]]
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
    assert tool_calls == ["search_documents"] and done["stop_reason"] == "time_budget"
    assert client.stream.call_count == 1
    payload = client.stream.call_args.kwargs["json"]
    assert "tools" not in payload
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_effort"] == "low"
    assert payload["messages"][-1] == {"role": "user", "content": chat_module._SEARCH_OVER_MESSAGE}
    assert payload["messages"][-2]["role"] == "tool", "it comes after the results the model is to answer from"
    assert text.startswith("From the results, $4,500.")
    saved = await _assistant_rows(db_session, conv.conversation_id)
    assert saved == [text], "the conversation keeps the answer, not the instruction"


@pytest.mark.asyncio
async def test_the_grace_runs_from_the_first_content_token_not_the_first_thinking_token(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model thinks for 100 s before its first word (Gemma 4 and gpt-oss both do, asked not to or not). The
    grace runs from the word: what it writes in the two minutes after that stands, and only what comes later is
    cut. Measured from the first line, the thinking would have spent the grace and the answer would be cut at its
    second word."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def move(n, i):
        if n != 1:
            return
        # after: thinking line 0 → t=351; thinking line 1 → t=401; content 0 (t=401) → t=520; content 1 → t=522
        clock.now = {0: 351.0, 1: 401.0, 2: 520.0, 3: 522.0}.get(i, clock.now)

    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [
                _reasoning_chunk("Let me see. "),
                _reasoning_chunk("The results say... "),
                _chunk(content="The fee "),
                _chunk(content="was $4,500."),
                _chunk(content=" Per month."),
                "data: [DONE]",
            ],
        ],
        on_line=move,
    )
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("The fee was $4,500."), text
    assert "Per month" not in text, "the word past the grace is cut"
    assert "Let me see" not in text, "thinking is not shown as the answer"


@pytest.mark.asyncio
async def test_thinking_alone_cannot_hold_the_forced_answer_open(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """A model that thinks regardless: past the thinking bound with no content yet, the call is cut, and content that
    starts later is not read. What it thought by then is what it produced, so that is shown, with the note; the
    app does not say the model wrote nothing when it did (gpt-oss writes its whole answer in that channel)."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def move(n, i):
        if n == 1 and i == 0:
            clock.now = 361.0
        if n == 1 and i == 1:
            clock.now = 301.0 + chat_module._FINAL_ANSWER_THINKING_SECONDS + 1.0

    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [
                _reasoning_chunk("Hmm. "),
                _reasoning_chunk("Still thinking. "),
                _chunk(content="Late words."),
                "data: [DONE]",
            ],
        ],
        on_line=move,
    )
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert "Late words" not in text, "content that starts past the thinking bound is cut"
    assert text.startswith("Hmm. Still thinking."), "what the model thought is what it produced"
    assert "I stopped after" in text
    assert "produced no answer" not in text, "the model wrote something, in its reasoning channel"


@pytest.mark.asyncio
async def test_an_answer_the_model_routed_through_its_reasoning_channel_is_the_answer(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """gpt-oss puts its whole answer in ``reasoning_content`` even asked not to think (research.py sees the same).
    An answer the model finished there (``finish_reason`` "stop") is not nothing: it is shown and stored, with the
    note."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [
                _reasoning_chunk("The fee was "),
                _reasoning_chunk("$4,500 a month."),
                _finish_chunk("stop"),
                "data: [DONE]",
            ],
        ]
    )
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("The fee was $4,500 a month.") and "I stopped after" in text
    assert "produced no answer" not in text
    saved = await _assistant_rows(db_session, conv.conversation_id)
    assert saved and saved[-1].startswith("The fee was $4,500 a month.")


@pytest.mark.asyncio
async def test_a_forced_call_that_produces_nothing_ends_in_the_apps_words_not_an_answer(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The time budget is spent and the forced call returns no text at all. What the user gets says the model
    produced no answer in the time allowed, in the app's voice: not the old first-person sentence that read as
    the model's answer, and not the note that it "answered from what it had found"."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"], ["data: [DONE]"]])
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget", "the done payload's contract with the harness stands"
    assert text == chat_module._nothing_produced_fallback("time_budget")
    assert text.startswith("_The model produced no answer in the time allowed")
    assert "wasn't able to formulate" not in text and "I stopped after" not in text
    saved = await _assistant_rows(db_session, conv.conversation_id)
    assert saved == [text]


@pytest.mark.asyncio
async def test_the_fallback_after_the_context_budget_names_the_context(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    monkeypatch.setattr(chat_module, "_TOOL_LOOP_BUDGET", 0.0)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"], ["data: [DONE]"]])
    text, done, _ = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert done["stop_reason"] == "context_budget"
    assert text == chat_module._nothing_produced_fallback("context_budget")
    assert text.startswith("_The model produced no answer in the context allowed")


@pytest.mark.parametrize("stop_reason", ["time_budget", "context_budget", "tool_rounds", None])
@pytest.mark.parametrize("server_failure", [None, "HTTP 500", "unreachable", "timed out"])
@pytest.mark.parametrize("prefix", ["", "Let me look. \n\n"])
def test_the_fallback_is_what_the_eval_harness_reads_as_no_answer(stop_reason, server_failure, prefix):
    """The sentence's opening is a contract with the harness's ``classify_answer``: the app's fallback is an
    empty answer there, never judged as the model's, whichever variant it is and even after a watched prefix. Both
    sides are in this repository, so the contract is tested from the app's side too."""
    from harbor_clerk.llm.chat import _nothing_produced_fallback
    from scripts.test_corpora.runner.quality import classify_answer

    label, reason = classify_answer(prefix + _nothing_produced_fallback(stop_reason, server_failure))
    assert label == "empty" and reason and "the model produced no answer" in reason


@pytest.mark.asyncio
async def test_the_forced_answers_clocks_start_at_the_models_first_line_not_at_a_keepalive(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The prompt is re-read before the forced call's first token, in silence, and keepalive sentinels arrive
    meanwhile. They start neither clock: a model whose first line comes 100 s into that silence gets its thinking
    bound from that line, and the answer it starts 25 s later stands."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "_KEEPALIVE_INTERVAL", 0.01)
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])

    async def silent_then_answer():
        await asyncio.sleep(0.05)  # sentinels flow while the prompt is re-read
        clock.now = 400.0
        yield _reasoning_chunk("A moment. ")
        clock.now = 425.0
        yield _chunk(content="The fee was $4,500.")
        yield _finish_chunk("stop")
        yield "data: [DONE]"

    _forced_response(client, silent_then_answer)
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("The fee was $4,500."), text


@pytest.mark.asyncio
async def test_a_running_grace_still_cuts_a_model_that_falls_silent(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The sentinel starts no clock, but a clock already running is checked on it: a model that wrote a word and
    then went quiet past its grace is cut at the keepalive, and the pending fetch is cancelled with it."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "_KEEPALIVE_INTERVAL", 0.01)
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    resumed: list[bool] = []

    async def one_word_then_silence():
        yield _chunk(content="Start ")
        clock.now = 301.0 + chat_module._FINAL_ANSWER_GRACE_SECONDS + 1.0  # the grace passes in silence
        await asyncio.sleep(0.2)
        resumed.append(True)  # only reached when the cut did not happen at the keepalive
        yield _chunk(content="Late.")
        yield _finish_chunk("stop")
        yield "data: [DONE]"

    _forced_response(client, one_word_then_silence)
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("Start ") and "Late" not in text
    assert resumed == [], "the cut came at the keepalive, not at the next line"


@pytest.mark.asyncio
async def test_a_thought_the_server_cut_short_is_shown_not_called_nothing(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model filled what was left of its context with reasoning and never reached its answer: llama-server ends
    the stream with ``finish_reason: "length"`` and "[DONE]" all the same. The thought is what it produced; it is
    shown rather than reported as nothing."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [_reasoning_chunk("The fee is in section 4, which"), _finish_chunk("length"), "data: [DONE]"],
        ]
    )
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("The fee is in section 4, which") and "I stopped after" in text
    assert "produced no answer" not in text


@pytest.mark.asyncio
async def test_the_fallback_after_the_tool_round_cap_names_the_cap(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    rounds = chat_module._MAX_TOOL_ROUNDS
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]] * rounds + [["data: [DONE]"]])
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert len(tool_calls) == rounds and done["stop_reason"] == "tool_rounds"
    assert text == chat_module._nothing_produced_fallback("tool_rounds")
    assert f"after {rounds} tool rounds" in text


@pytest.mark.asyncio
async def test_a_finished_answer_whose_finish_chunk_arrives_at_the_thinking_bound_is_kept(
    db_session, admin_user, chat_session_factory, monkeypatch, caplog
):
    """gpt-oss wrote its whole answer in its reasoning channel and finished; the finish chunk and "[DONE]" arrive at
    the thinking bound. They carry no token, so they are let through and nothing is cut: the answer stands as a
    finished one, not as a thought cut short."""
    import logging

    from harbor_clerk.llm import chat as chat_module

    caplog.set_level(logging.INFO, logger="harbor_clerk.llm.chat")
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def move(n, i):
        if n == 1 and i == 1:
            clock.now = 301.0 + chat_module._FINAL_ANSWER_THINKING_SECONDS

    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [
                _reasoning_chunk("The fee was "),
                _reasoning_chunk("$4,500 a month."),
                _finish_chunk("stop"),
                "data: [DONE]",
            ],
        ],
        on_line=move,
    )
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text.startswith("The fee was $4,500 a month.") and "I stopped after" in text
    assert "produced no answer" not in text
    assert "Final answer cut" not in caplog.text, "the finish chunk cut nothing"
    assert "finish_reason=stop, cut=None" in caplog.text


@pytest.mark.asyncio
async def test_a_forced_call_the_server_fails_is_said_to_have_failed_not_to_have_written_nothing(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The forced call comes back 500: the model did not write nothing, the server failed. The sentence says which,
    with the status, and the failure reaches the health indicator."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    failed = MagicMock()
    failed.status_code = 500
    failed.text = "slot unavailable"
    failed.aread = AsyncMock()
    failed.aclose = AsyncMock()
    client.stream.return_value.__aenter__ = AsyncMock(return_value=failed)
    reported: list[int] = []
    monkeypatch.setattr(chat_module, "report_llm_error", reported.append)
    text, done, _ = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        budget=300.0,
        on_tool=lambda: setattr(clock, "now", 301.0),
    )
    assert done["stop_reason"] == "time_budget"
    assert text == chat_module._nothing_produced_fallback("time_budget", "HTTP 500")
    assert text.startswith("_The model server failed while the final answer was being written (HTTP 500)")
    assert "wrote nothing" not in text and "I stopped after" not in text
    assert reported == [500]


@pytest.mark.asyncio
async def test_a_search_cut_before_any_result_is_told_so_not_to_answer_from_results_above(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The first generation outlasts the budget: no tool result exists. The forced call's last message must not
    point at "the results above"."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="I could not search."), "data: [DONE]"]],
        on_send=lambda n: setattr(clock, "now", 301.0) if n == 0 else None,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == [] and done["stop_reason"] == "time_budget"
    payload = client.stream.call_args.kwargs["json"]
    assert not any(m["role"] == "tool" for m in payload["messages"])
    assert payload["messages"][-1] == {"role": "user", "content": chat_module._SEARCH_OVER_UNSEARCHED_MESSAGE}
    assert text.startswith("I could not search.")


@pytest.mark.asyncio
async def test_a_model_that_returns_nothing_unforced_gets_the_plain_fallback_and_no_stop_reason(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The very first generation ends with neither a tool call nor text. No search was stopped, so there is no
    stop_reason and no forced call; the app still says, in its own words, that the model produced no answer."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([["data: [DONE]"]])
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=300.0)
    assert tool_calls == [] and "stop_reason" not in done
    assert client.stream.call_count == 0, "nothing was forced"
    assert text == chat_module._nothing_produced_fallback(None)
    assert text.startswith("_The model produced no answer: it returned nothing for this question.")
