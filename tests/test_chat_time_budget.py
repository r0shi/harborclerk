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


def _pin_no_model(monkeypatch) -> None:
    """No active model for the run, whatever this machine's native config names: ``chat_stream`` re-reads it
    (``refresh_llm_settings``), so the ratio table's key is ``_MODEL_KEY`` and the tool schema is the bare one."""
    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(get_settings(), "llm_model_id", "")
    monkeypatch.setattr(chat_module, "refresh_llm_settings", lambda: None)


async def _conversation(db_session, admin_user):
    from harbor_clerk.models.conversation import Conversation

    conv = Conversation(user_id=admin_user.user_id, title="Bounded chat", scope=None)
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


async def _drive(
    conv_id,
    admin_user,
    client,
    clock,
    monkeypatch,
    budget: float,
    on_tool=None,
    *,
    result: str | None = None,
    context_tokens: int | None = None,
    ratios: dict | None = None,
):
    """Drive one chat turn. ``ratios`` is the process's table of measured estimate ratios for the run (#805), empty
    unless given, so no test starts from another's measurements; the caller keeps the reference to read it after."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", budget)
    monkeypatch.setattr(chat_module, "_now", clock)
    monkeypatch.setattr(chat_module, "_estimate_ratios", {} if ratios is None else ratios)
    _pin_no_model(monkeypatch)
    if context_tokens is not None:
        monkeypatch.setattr(chat_module, "context_budget", lambda model, yarn: context_tokens)
    tool_calls: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        tool_calls.append(fn_name)
        if on_tool:
            on_tool()
        return result if result is not None else json.dumps({"results": [], "total": 0})

    async def run():
        events = []
        with (
            patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
            patch("httpx.AsyncClient", return_value=client),
        ):
            async for raw in chat_module.chat_stream(conv_id, "What did the Q3 contract cost?", admin_user.user_id):
                if raw.startswith("data: "):
                    events.append(json.loads(raw[6:]))
        return events

    events = await asyncio.wait_for(run(), timeout=30.0)
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


# The key the chat loop records its measured estimate ratio under: the active model id, "" when none (#805). The
# drivers pin the run to no model (``_pin_no_model``), so the key is the empty string whatever this machine's config.
_MODEL_KEY = ""


def _usage_chunk(prompt_tokens: int, completion_tokens: int = 5) -> str:
    """The stream's last chunk at the pin (v0.4.1) when the request asks for usage: no choices at all, and the
    server's count of the whole prompt."""
    return "data: " + json.dumps(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


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
    starts later is not read. What it thought by then is what it produced, so that is shown, under the app's lead
    that says so and without the note that calls it an answer; the app does not say the model wrote nothing when it
    did (gpt-oss writes its whole answer in that channel)."""
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
    assert text == chat_module._cut_thought_answer("Hmm. Still thinking. ", "time")
    assert text.startswith(
        "_The model was still reasoning when its time ran out; what it had:_\n\nHmm. Still thinking."
    )
    assert "I stopped after" not in text, "the lead says the time ran out; the note would call the thought an answer"
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
    assert label == "empty" and reason
    assert ("the model server failed" if server_failure else "the model produced no answer") in reason


@pytest.mark.parametrize("ran_out", ["time", "context"])
@pytest.mark.parametrize("prefix", ["", "Let me look. \n\n"])
def test_a_cut_thought_is_what_the_eval_harness_reads_as_no_answer(ran_out, prefix):
    from harbor_clerk.llm.chat import _cut_thought_answer
    from scripts.test_corpora.runner.quality import classify_answer

    label, reason = classify_answer(prefix + _cut_thought_answer("The fee is in section 4, which", ran_out))
    assert label == "empty" and reason and "still reasoning" in reason


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
    shown under the app's lead rather than reported as nothing or passed off as an answer."""
    from harbor_clerk.llm import chat as chat_module

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
    assert text == chat_module._cut_thought_answer("The fee is in section 4, which", "context")
    assert "I stopped after" not in text and "produced no answer" not in text


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


@pytest.mark.asyncio
async def test_blank_content_before_a_reasoning_answer_does_not_hide_the_answer(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """A reasoning parser commonly streams "\\n\\n" as content once it has stripped a closing think tag, then the
    answer in the reasoning channel. Blank content is no content: the finished reasoning answer is the answer."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [
            [_tool_call_chunk(), "data: [DONE]"],
            [
                _chunk(content="\n\n"),
                _reasoning_chunk("The fee was $4,500 a month."),
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
    assert text.strip().startswith("The fee was $4,500 a month.") and "I stopped after" in text
    assert "produced no answer" not in text and "still reasoning" not in text


@pytest.mark.asyncio
async def test_blank_content_and_nothing_else_is_nothing(db_session, admin_user, chat_session_factory, monkeypatch):
    """The forced call streams only "\\n\\n": the app's sentence, not a blank answer with the note."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="\n\n"), _finish_chunk("stop"), "data: [DONE]"]]
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
    assert text.strip() == chat_module._nothing_produced_fallback("time_budget")
    assert "I stopped after" not in text


@pytest.mark.asyncio
async def test_blank_content_before_the_deadline_does_not_grant_the_grace_to_what_follows(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model streams "\\n\\n" inside the budget and its first real token after the deadline. Blank text is not
    an answer being written, so the generation is cut at the deadline like any other and the answer is forced; it
    does not run on under the grace an answer in progress would get."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client(
        [
            [_chunk(content="\n\n"), _chunk(content="The fee was $4,500."), "data: [DONE]"],
            [_chunk(content="From the results, $4,500."), "data: [DONE]"],
        ],
        on_line=lambda n, i: setattr(clock, "now", 301.0) if (n == 0 and i == 0) else None,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == [] and done["stop_reason"] == "time_budget"
    assert client.stream.call_count == 1, "the generation was cut at the deadline and the answer forced"
    assert "The fee was" not in text and text.strip().startswith("From the results, $4,500.")


@pytest.mark.asyncio
async def test_an_earlier_turns_results_do_not_count_as_this_turns_search(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The conversation searched in an earlier turn, so its history holds tool rows. This turn is cut before any
    result: the forced call must not point the model at "the results above", which are the old turn's."""
    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    call = [{"id": "call_0", "type": "function", "function": {"name": "search_documents", "arguments": "{}"}}]
    for msg in (
        ChatMessage(conversation_id=conv.conversation_id, role="user", content="Earlier question?"),
        ChatMessage(conversation_id=conv.conversation_id, role="assistant", content="", tool_calls=call),
        ChatMessage(
            conversation_id=conv.conversation_id, role="tool", content='{"results": []}', tool_call_id="call_0"
        ),
        ChatMessage(conversation_id=conv.conversation_id, role="assistant", content="Earlier answer."),
    ):
        db_session.add(msg)
    await db_session.commit()
    clock = _Clock()
    client, _ = _mock_client(
        [[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="I could not search."), "data: [DONE]"]],
        on_send=lambda n: setattr(clock, "now", 301.0) if n == 0 else None,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert tool_calls == [] and done["stop_reason"] == "time_budget"
    payload = client.stream.call_args.kwargs["json"]
    assert any(m["role"] == "tool" for m in payload["messages"]), "the old turn's results are in the history"
    assert payload["messages"][-1] == {"role": "user", "content": chat_module._SEARCH_OVER_UNSEARCHED_MESSAGE}


# ── #774 and #773: the forced final call fits the context, and is bounded from the moment the search stopped ──


_REFUSAL_BODY = (
    '{{"error":{{"code":400,"message":"request ({counted} tokens) exceeds the available context size '
    '({ctx} tokens), try increasing it","type":"exceed_context_size_error"}}}}'
)


def _refusal(counted: int, ctx: int) -> MagicMock:
    """llama-server's refusal of a prompt longer than the slot's context, as the pin words it."""
    resp = MagicMock()
    resp.status_code = 400
    resp.text = _REFUSAL_BODY.format(counted=counted, ctx=ctx)
    resp.aread = AsyncMock()
    resp.aclose = AsyncMock()
    return resp


def _lines_response(lines: list[str]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200

    async def aiter_lines():
        for line in lines:
            yield line

    resp.aiter_lines = aiter_lines
    resp.aclose = AsyncMock()
    return resp


def _bounded(fn, *, seconds: float):
    """``fn()`` on a daemon thread, or a failure after ``seconds``: a pure function that spins must fail, not hang.
    On failure the thread is left spinning until the process exits (a daemon thread cannot be stopped); the one
    test that uses this fails loudly in that case, which is the point."""
    import threading

    box: list = []
    thread = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"did not return within {seconds}s"
    return box[0]


def _big_search_result(n: int = 60) -> str:
    return json.dumps(
        {
            "results": [
                {"chunk_id": f"0000{i:04d}-aaaa-bbbb-cccc-ddddeeeeffff", "text": f"Clause {i}: " + "fee " * 40}
                for i in range(n)
            ],
            "total": n,
        }
    )


async def _drive_context_stop(
    conv_id, admin_user, client, clock, monkeypatch, *, context_tokens: int, budget: float, result: str | None = None
):
    """A search that stops at the context budget after one round, with a result at the truncation cap (or ``result``),
    on a model whose context is ``context_tokens``."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "context_budget", lambda model, yarn: context_tokens)
    monkeypatch.setattr(chat_module, "_TOOL_LOOP_BUDGET", 0.0)  # any usage at all stops the search after round one
    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", budget)
    monkeypatch.setattr(chat_module, "_now", clock)
    monkeypatch.setattr(chat_module, "_estimate_ratios", {})  # no measurement from another test (#805)
    _pin_no_model(monkeypatch)

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        return result if result is not None else _big_search_result()

    async def run():
        events = []
        with (
            patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
            patch("httpx.AsyncClient", return_value=client),
        ):
            async for raw in chat_module.chat_stream(conv_id, "What did the Q3 contract cost?", admin_user.user_id):
                if raw.startswith("data: "):
                    events.append(json.loads(raw[6:]))
        return events

    events = await asyncio.wait_for(run(), timeout=30.0)
    text = "".join(e.get("content", "") for e in events if e.get("type") == "token")
    done = next(e for e in events if e.get("type") == "done")
    return text, done


@pytest.mark.asyncio
async def test_the_forced_prompt_goes_as_it_stands_and_the_stored_row_is_the_full_result(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The search stopped at the context budget with a result at the truncation cap. The forced prompt is the loop's
    messages plus the "search is over" message, not a prompt trimmed on the estimate: the server, not the estimate,
    says whether it fits (the retry below). The stored tool row is the full result."""
    from sqlalchemy import select

    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"], [_chunk(content="From what fit."), "data: [DONE]"]])
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=4096, budget=0.0
    )
    assert done["stop_reason"] == "context_budget" and text == "From what fit."
    assert client.stream.call_count == 1

    loop_messages = client.build_request.call_args.kwargs["json"]["messages"]
    forced = client.stream.call_args.kwargs["json"]["messages"]
    assert forced == [*loop_messages, {"role": "user", "content": chat_module._SEARCH_OVER_MESSAGE}]

    rows = (
        (await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id)))
        .scalars()
        .all()
    )
    stored = next(r.content for r in rows if r.role == "tool")
    assert stored == _big_search_result(), "the conversation keeps the full result"


def test_a_forced_prompt_that_fits_is_sent_as_it_is():
    from harbor_clerk.llm import chat as chat_module

    messages = [
        {"role": "system", "content": "s" * 350},
        {"role": "user", "content": "q"},
        {"role": "tool", "content": _big_search_result(5), "tool_call_id": "1"},
    ]
    assert chat_module._trim_for_forced_call(messages, 100_000) == messages


def test_trimming_shrinks_the_largest_result_first_and_stops_at_the_floor():
    """Halving the largest result first, down to the floor; a result that cannot be parsed is cut plainly, with a
    marker, and shorter every time (``_truncate_for_llm`` alone returned a near-floor result no shorter, which would
    not have terminated). An estimate still over when every result is at the floor is sent as it is."""
    from harbor_clerk.llm import chat as chat_module

    small = _big_search_result(5)
    large = chat_module._truncate_for_llm(_big_search_result(60), 12_000)  # as the loop leaves it: cut once already
    assert json.loads(large)["_original_count"] == 60 and len(json.loads(large)["results"]) < 60
    messages = [
        {"role": "system", "content": "s" * 3500},
        {"role": "tool", "content": small, "tool_call_id": "1"},
        {"role": "tool", "content": large, "tool_call_id": "2"},
        {"role": "tool", "content": "x" * 450, "tool_call_id": "3"},
    ]
    budget = chat_module._estimate_messages_tokens(messages) - 1000
    trimmed = chat_module._trim_for_forced_call(messages, budget)
    assert chat_module._estimate_messages_tokens(trimmed) <= budget
    assert trimmed[1]["content"] == small, "the small result was not touched"
    recut = json.loads(trimmed[2]["content"])
    assert len(trimmed[2]["content"]) < len(large) and recut["_truncated"] is True
    assert recut["_original_count"] == 60, "the second cut keeps the count the search returned, not the first cut's"
    assert messages[2]["content"] == large, "the input is not changed"

    # The near-floor case, directly: the structured cut alone comes back no shorter, so a trim built on it would spin.
    assert len(chat_module._truncate_for_llm("x" * 450, 400)) > 400
    assert len(chat_module._shrink_result("x" * 450, 400)) <= 400

    floor = chat_module._FORCED_CALL_RESULT_FLOOR_CHARS
    unreachable = _bounded(lambda: chat_module._trim_for_forced_call(messages, 10), seconds=5.0)
    assert all(len(m["content"]) <= floor for m in unreachable if m["role"] == "tool")
    assert unreachable[3]["content"].endswith("[cut to fit the context]")
    assert unreachable[0]["content"] == messages[0]["content"], "the system prompt is never cut"


def test_the_servers_refusal_is_recognised_and_its_count_read():
    from harbor_clerk.llm import chat as chat_module

    body = _REFUSAL_BODY.format(counted=18224, ctx=16384)
    assert chat_module._is_context_overflow(400, body)
    assert chat_module._refused_prompt_tokens(body) == 18224
    assert chat_module._refused_prompt_tokens('{"n_prompt_tokens": 19240, "n_ctx": 16384}') == 19240
    assert chat_module._refused_prompt_tokens("the request exceeds the available context size") is None
    assert chat_module._refused_context_tokens(body) == 16384
    assert chat_module._refused_context_tokens('{"n_prompt_tokens": 19240, "n_ctx": 16384}') == 16384
    assert chat_module._refused_context_tokens("the request exceeds the available context size") is None
    assert not chat_module._is_context_overflow(500, body), "a 500 is a server failure, whatever it says"
    assert not chat_module._is_context_overflow(400, '{"error": "invalid json body"}')


@pytest.mark.asyncio
async def test_a_forced_call_refused_as_too_long_is_retried_once_with_a_prompt_cut_to_the_servers_count(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The estimate said the prompt fit; llama-server counted three times as many tokens and refused it. The refusal
    names its count, so the prompt is trimmed against that count, with the reserve, and sent once more; the answer
    comes from the second call and the user never sees a server failure."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    ctx = 8192
    served: list = []

    async def serve(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        served.append(payload["messages"])
        if len(served) == 1:
            estimate = chat_module._estimate_messages_tokens(payload["messages"])
            assert estimate * 3 > ctx, "the scenario: the server's count is over the context"
            return _refusal(estimate * 3, ctx)
        return _lines_response([_chunk(content="From the results, $4,500."), "data: [DONE]"])

    client.stream.return_value.__aenter__ = AsyncMock(side_effect=serve)
    reported: list[int] = []
    monkeypatch.setattr(chat_module, "report_llm_error", reported.append)
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=ctx, budget=0.0
    )
    assert done["stop_reason"] == "context_budget"
    assert text == "From the results, $4,500.", text
    assert client.stream.call_count == 2, "refused once, retried once"
    assert "model server failed" not in text
    assert reported == [], "a 400 is not a server failure"
    first, second = (chat_module._estimate_messages_tokens(m) for m in served)
    assert second < first, "the retry's prompt is shorter"
    counted = first * 3
    assert second * (counted / first) <= ctx * (1 - chat_module._RESPONSE_RESERVE), (
        "at the server's rate, the retry fits with the response reserve"
    )
    assert served[1][-1] == {"role": "user", "content": chat_module._SEARCH_OVER_MESSAGE}
    assert all(m["role"] != "tool" or m["content"] for m in served[1]), "every result still answers its call"


@pytest.mark.asyncio
async def test_a_refusal_at_a_rate_halving_would_not_cover_is_trimmed_to_the_servers_count(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """On a 16K model the server counts eight times the estimate. Halving the prompt would leave the retry at
    about 2,500 estimated tokens, 20,000 by the server: refused again. Calibrated to the count, the retry is
    trimmed to what fits with the response reserve at the server's rate."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    ctx, ratio = 16384, 8
    served: list = []

    async def serve(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        served.append(payload["messages"])
        if len(served) == 1:
            estimate = chat_module._estimate_messages_tokens(payload["messages"])
            assert (estimate // 2) * ratio > ctx, "the scenario: halving alone would not fit"
            return _refusal(estimate * ratio, ctx)
        return _lines_response([_chunk(content="From the results, $4,500."), "data: [DONE]"])

    client.stream.return_value.__aenter__ = AsyncMock(side_effect=serve)
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=ctx, budget=0.0
    )
    assert done["stop_reason"] == "context_budget" and text == "From the results, $4,500."
    assert client.stream.call_count == 2
    first, second = (chat_module._estimate_messages_tokens(m) for m in served)
    calibrated = int(ctx * (1 - chat_module._RESPONSE_RESERVE) / (ratio * chat_module._FORCED_CALL_RETRY_SAFETY))
    assert second <= calibrated, "trimmed to the calibrated target"
    assert second * ratio <= ctx * (1 - chat_module._RESPONSE_RESERVE), "fits with the reserve at the server's rate"
    assert second > first // 4, "and not to the floor: the result is halved until it fits, not discarded"


@pytest.mark.asyncio
async def test_a_retry_is_calibrated_to_the_context_the_server_states_not_the_registrys(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """Under Compose nothing clamps the registry's window to the server's -c (#654): the client budgets at 32,768
    while llama-server runs at 16,384. The refusal states the slot's context, so the retry is trimmed to fit that
    one; calibrated to the registry's it would be refused again, the outcome #774 set out to remove."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    registry_ctx, server_ctx, ratio = 32768, 16384, 3
    served: list = []

    async def serve(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        served.append(payload["messages"])
        if len(served) == 1:
            estimate = chat_module._estimate_messages_tokens(payload["messages"])
            assert estimate * ratio > server_ctx
            return _refusal(estimate * ratio, server_ctx)
        return _lines_response([_chunk(content="From the results, $4,500."), "data: [DONE]"])

    client.stream.return_value.__aenter__ = AsyncMock(side_effect=serve)
    text, done = await _drive_context_stop(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        context_tokens=registry_ctx,
        budget=0.0,
        result=_big_search_result(150),  # at the 32K truncation cap: about 9,100 estimated tokens
    )
    assert done["stop_reason"] == "context_budget" and text == "From the results, $4,500."
    assert client.stream.call_count == 2
    first, second = (chat_module._estimate_messages_tokens(m) for m in served)
    reserve, safety = chat_module._RESPONSE_RESERVE, chat_module._FORCED_CALL_RETRY_SAFETY
    assert second <= int(server_ctx * (1 - reserve) / (ratio * safety)), "trimmed to the server's context"
    assert second * ratio <= server_ctx * (1 - reserve), "fits the server's slot with the reserve"
    assert second > first // 4, "and not to the floor"


@pytest.mark.asyncio
async def test_a_retry_the_model_answers_with_nothing_is_the_models_nothing_not_the_servers_failure(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The refusal is retried and accepted, and the model then streams nothing. The server did not fail: the
    sentence is the one for a model that wrote nothing, not the HTTP 400 one, and the harness reads it as such."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    client.stream.return_value.__aenter__ = AsyncMock(
        side_effect=[_refusal(60_000, 32768), _lines_response(["data: [DONE]"])]
    )
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=32768, budget=0.0
    )
    assert done["stop_reason"] == "context_budget" and client.stream.call_count == 2
    assert text == chat_module._nothing_produced_fallback("context_budget")
    assert "HTTP 400" not in text and "model server failed" not in text


@pytest.mark.asyncio
async def test_a_refusal_without_a_count_halves_the_prompt(db_session, admin_user, chat_session_factory, monkeypatch):
    """A build whose refusal names no count ("the request exceeds the available context size"): nothing to
    calibrate on, so the prompt is halved, the largest result first."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    uncounted = MagicMock()
    uncounted.status_code = 400
    uncounted.text = (
        '{"error":{"code":400,"message":"the request exceeds the available context size, try increasing it"}}'
    )
    uncounted.aread = AsyncMock()
    uncounted.aclose = AsyncMock()
    served: list = []

    async def serve(*_a, **_k):
        served.append(client.stream.call_args.kwargs["json"]["messages"])
        if len(served) == 1:
            return uncounted
        return _lines_response([_chunk(content="Halved."), "data: [DONE]"])

    client.stream.return_value.__aenter__ = AsyncMock(side_effect=serve)
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=32768, budget=0.0
    )
    assert done["stop_reason"] == "context_budget" and text == "Halved." and client.stream.call_count == 2
    first, second = (chat_module._estimate_messages_tokens(m) for m in served)
    assert first // 4 <= second <= first // 2, f"about half: {first} -> {second}"


@pytest.mark.asyncio
async def test_a_forced_call_refused_twice_falls_to_the_server_failure_sentence(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The retry is refused too (the floor was reached, or the server's count was wrong): one retry, then the
    existing sentence that says the model server failed, with the status."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    client.stream.return_value.__aenter__ = AsyncMock(
        side_effect=[_refusal(40_000, 32768), _refusal(39_000, 32768), _refusal(38_000, 32768)]
    )
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=32768, budget=0.0
    )
    assert done["stop_reason"] == "context_budget"
    assert client.stream.call_count == 2, "one retry, not a loop"
    assert text == chat_module._nothing_produced_fallback("context_budget", "HTTP 400")
    assert text.startswith("_The model server failed while the final answer was being written (HTTP 400)")


@pytest.mark.asyncio
async def test_a_refusal_with_nothing_left_to_trim_is_not_retried(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The only tool result is already under the floor, so a harder trim changes nothing: the same prompt is not
    sent twice. The refusal is reported as the server failure it is."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    client.stream.return_value.__aenter__ = AsyncMock(side_effect=[_refusal(40_000, 32768), _refusal(39_000, 32768)])
    text, done = await _drive_context_stop(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        context_tokens=32768,
        budget=0.0,
        result=json.dumps({"results": [], "total": 0}),
    )
    assert done["stop_reason"] == "context_budget"
    assert client.stream.call_count == 1, "nothing shorter to send"
    assert text == chat_module._nothing_produced_fallback("context_budget", "HTTP 400")


def test_the_outer_deadline_allows_for_the_prompts_re_read_at_the_slowest_measured_rate():
    """#773's figures: a forced call at 18,000 tokens of context (about 9,000 estimated) on Qwen3.8-27B spent five
    and a half minutes being read before either clock started. The outer deadline, from the moment the search
    stopped, is that read at the slowest registered model's rate, then the thinking bound, then the grace."""
    from harbor_clerk.llm import chat as chat_module

    ratio = chat_module._ESTIMATE_RATIO_DEFAULT  # what an uncounted prompt is read at (#805)
    predicted = int(9000 * ratio)
    allowance = predicted / chat_module._PREFILL_TOKENS_PER_SECOND
    assert chat_module._PREFILL_TOKENS_PER_SECOND <= 56, "no registered model prefills slower than the 27B's 56/s"
    assert allowance >= 18000 / 56, "the allowance covers the measured read"
    expected = chat_module._forced_call_outer_deadline(301.0, predicted)
    assert expected >= 301.0 + 18000 / 56 + chat_module._FINAL_ANSWER_THINKING_SECONDS, (
        "the read at the measured rate and the thinking bound both fit before it"
    )
    assert chat_module._forced_answer_cut(expected, None, None, expected) == "outer"
    assert chat_module._forced_answer_cut(expected - 1, None, None, expected) is None
    assert chat_module._forced_answer_cut(expected, 10_000.0, 10_000.0, expected) == "outer", (
        "the outer bound holds whatever the inner clocks say"
    )


@pytest.mark.asyncio
async def test_the_outer_deadline_cuts_a_forced_call_whose_first_token_never_arrives(
    db_session, admin_user, chat_session_factory, monkeypatch, caplog
):
    """The prompt is being re-read and no token comes: neither inner clock has started, and before #773 nothing
    bounded the wait. The outer deadline, from the moment the search stopped, cuts the call at a keepalive, with
    the app's existing sentence for a model that wrote nothing in the time it had."""
    import asyncio
    import logging

    from harbor_clerk.llm import chat as chat_module

    caplog.set_level(logging.INFO, logger="harbor_clerk.llm.chat")
    monkeypatch.setattr(chat_module, "_KEEPALIVE_INTERVAL", 0.01)
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    never = asyncio.Event()
    resumed: list[bool] = []

    async def silent_forever():
        payload = client.stream.call_args.kwargs["json"]
        # No usage came from the loop's call, so the prompt is read at the default ratio, whole.
        outer = chat_module._forced_call_outer_deadline(
            301.0,
            int(chat_module._estimate_ratio(_MODEL_KEY) * chat_module._estimate_messages_tokens(payload["messages"])),
        )
        clock.now = outer + 1.0  # the read outlasts the allowance, the thinking bound and the grace together
        await never.wait()
        resumed.append(True)
        yield _chunk(content="Never.")

    _forced_response(client, silent_forever)
    text, done, _ = await asyncio.wait_for(
        _drive(
            conv.conversation_id,
            admin_user,
            client,
            clock,
            monkeypatch,
            budget=300.0,
            on_tool=lambda: setattr(clock, "now", 301.0),
        ),
        timeout=10.0,
    )
    assert done["stop_reason"] == "time_budget"
    assert text == chat_module._nothing_produced_fallback("time_budget")
    assert text.startswith("_The model produced no answer in the time allowed")
    assert resumed == [], "the cut came at the keepalive"
    assert "Final answer cut at its outer bound" in caplog.text


@pytest.mark.asyncio
async def test_a_first_content_token_past_the_outer_deadline_is_cut_at_the_token(
    db_session, admin_user, chat_session_factory, monkeypatch, caplog
):
    """No keepalive this time: the model's first content token itself arrives after the outer deadline. It is
    cut there, not read as the start of a grace, and the user gets the sentence for nothing produced in time."""
    import asyncio
    import logging

    from harbor_clerk.llm import chat as chat_module

    caplog.set_level(logging.INFO, logger="harbor_clerk.llm.chat")
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])

    async def first_token_too_late():
        payload = client.stream.call_args.kwargs["json"]
        # No usage came from the loop's call, so the prompt is read at the default ratio, whole.
        outer = chat_module._forced_call_outer_deadline(
            301.0,
            int(chat_module._estimate_ratio(_MODEL_KEY) * chat_module._estimate_messages_tokens(payload["messages"])),
        )
        clock.now = outer + 1.0
        yield _chunk(content="Too late.")
        yield _finish_chunk("stop")
        yield "data: [DONE]"

    _forced_response(client, first_token_too_late)
    text, done, _ = await asyncio.wait_for(
        _drive(
            conv.conversation_id,
            admin_user,
            client,
            clock,
            monkeypatch,
            budget=300.0,
            on_tool=lambda: setattr(clock, "now", 301.0),
        ),
        timeout=10.0,
    )
    assert done["stop_reason"] == "time_budget"
    assert "Too late" not in text
    assert text == chat_module._nothing_produced_fallback("time_budget")
    assert "Final answer cut at its outer bound" in caplog.text


@pytest.mark.asyncio
async def test_a_first_token_inside_the_prefill_allowance_is_not_cut_by_the_outer_bound(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """A slow model with a large prompt (a result at the 32K truncation cap, about 9,000 estimated tokens, so an
    allowance of about six minutes): its first token comes after the thinking bound and the grace have both run
    out, counted from the stop, but inside the allowance for the read. The answer it then writes stands."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()  # the search stops at t=0: the clock does not move during the single round
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    inner_bounds = chat_module._FINAL_ANSWER_THINKING_SECONDS + chat_module._FINAL_ANSWER_GRACE_SECONDS

    async def late_but_allowed():
        payload = client.stream.call_args.kwargs["json"]
        estimate = chat_module._estimate_messages_tokens(payload["messages"])
        ratio = chat_module._estimate_ratio(_MODEL_KEY)  # uncounted here: the loop's one call reported no usage
        allowance = estimate * ratio / chat_module._PREFILL_TOKENS_PER_SECOND
        assert allowance > inner_bounds + 1.0, "the scenario: the read alone outlasts both inner bounds"
        clock.now = inner_bounds + 1.0
        yield _chunk(content="The fee was $4,500.")
        clock.now += 60.0
        yield _finish_chunk("stop")
        yield "data: [DONE]"

    _forced_response(client, late_but_allowed)
    text, done = await _drive_context_stop(
        conv.conversation_id,
        admin_user,
        client,
        clock,
        monkeypatch,
        context_tokens=32768,
        budget=300.0,
        result=_big_search_result(150),  # over the cap: the loop truncates it to the cap, about 8,200 tokens
    )
    assert done["stop_reason"] == "context_budget"
    assert text == "The fee was $4,500.", text


@pytest.mark.asyncio
async def test_the_tool_loops_overflow_error_recognises_the_pins_wording(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The tool loop's own 400 handling (older than the forced call's) shares the detection: llama-server's
    "request (N tokens) exceeds the available context size" is reported as the context being full, not as a bare
    LLM error."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_chunk(content="never streamed"), "data: [DONE]"]])
    client.send = AsyncMock(return_value=_refusal(18224, 16384))
    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", 0.0)

    async def run():
        events = []
        with patch("httpx.AsyncClient", return_value=client):
            async for raw in chat_module.chat_stream(conv.conversation_id, "Too long?", admin_user.user_id):
                if raw.startswith("data: "):
                    events.append(json.loads(raw[6:]))
        return events

    events = await asyncio.wait_for(run(), timeout=10.0)
    error = next(e for e in events if e.get("type") == "error")
    assert error["message"] == "Context window full — please start a new conversation"
    assert "exceeds the available context" in error["detail"]
    done = next(e for e in events if e.get("type") == "done")
    assert done["context_pct"] == 100


# ── #805: the estimate is calibrated against the server's count ──────────────────────────────────────────────────────


def _counting_client(ratio: float, rounds: int | None = None, forced_ratio: float | None = None):
    """A model that calls a tool every round (the first ``rounds`` rounds, when given, then answers), on a server
    that counts each prompt at ``ratio`` times the raw estimate of what was sent and reports the count as usage, in
    a last chunk with no choices (the pin's shape). The forced final call is answered the same way, counted at
    ``forced_ratio`` (``ratio`` when not given), finish chunk and usage chunk before "[DONE]". Returns the client
    and the payloads the loop sent, in order."""
    from harbor_clerk.llm import chat as chat_module

    sent: list[dict] = []

    def forced(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        counted = int((forced_ratio or ratio) * chat_module._estimate_messages_tokens(payload["messages"]))
        return _lines_response(
            [_chunk(content="From the results."), _finish_chunk("stop"), _usage_chunk(counted), "data: [DONE]"]
        )

    def build(*_a, **_k):
        payload = client.build_request.call_args.kwargs["json"]
        sent.append({**payload, "messages": list(payload["messages"])})  # a snapshot: the loop appends to its list
        counted = int(ratio * chat_module._estimate_payload_tokens(payload["messages"], payload.get("tools")))
        if rounds is not None and len(sent) > rounds:
            return _lines_response([_chunk(content="Answered on my own."), _usage_chunk(counted), "data: [DONE]"])
        return _lines_response([_tool_call_chunk(), _usage_chunk(counted), "data: [DONE]"])

    client = MagicMock()
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(side_effect=build)
    client.aclose = AsyncMock()
    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(side_effect=forced)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_ctx)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, sent


def test_the_ratio_is_measured_clamped_and_smoothed():
    """A first measurement replaces the default; a later one is blended in at the smoothing weight; a measurement
    is clamped before it is blended; a count or an estimate that is not positive records nothing."""
    from harbor_clerk.llm import chat as chat_module

    table: dict = {}
    with patch.object(chat_module, "_estimate_ratios", table):
        default, top, bottom = (
            chat_module._ESTIMATE_RATIO_DEFAULT,
            chat_module._ESTIMATE_RATIO_MAX,
            chat_module._ESTIMATE_RATIO_MIN,
        )
        assert chat_module._estimate_ratio("m") == default
        assert chat_module._record_estimate_ratio("m", 3000, 1000) == pytest.approx((default + 3.0) / 2), (
            "the first measurement is blended from the default, not a replacement"
        )
        assert chat_module._estimate_ratio("m") == pytest.approx(2.5)
        assert chat_module._record_estimate_ratio("m", 1000, 1000) == pytest.approx(1.75), "half way to the newest"
        assert chat_module._record_estimate_ratio("m", 10_000_000, 1000) <= top
        assert chat_module._record_estimate_ratio("m", 1, 1_000_000) >= bottom
        assert chat_module._record_estimate_ratio("other", 10_000_000, 1) == pytest.approx((default + top) / 2), (
            "the absurd count moves the ratio half way to the clamp, not off the scale"
        )
        assert chat_module._record_estimate_ratio("other", 1, 10_000_000) == pytest.approx(
            ((default + top) / 2 + bottom) / 2
        )
        assert chat_module._record_estimate_ratio("fresh", 0, 1000) is None
        assert chat_module._record_estimate_ratio("fresh", 1000, 0) is None
        assert "fresh" not in table and chat_module._estimate_ratio("fresh") == chat_module._ESTIMATE_RATIO_DEFAULT
        assert chat_module._estimate_ratio(None) == chat_module._ESTIMATE_RATIO_DEFAULT
        assert chat_module._record_estimate_ratio(None, 1500, 1000) == pytest.approx((default + 1.5) / 2)
        assert table[""] == pytest.approx((default + 1.5) / 2), "no active model is keyed as the empty string"


def test_the_trim_keeps_the_system_prompt_and_the_question_when_nothing_else_fits():
    """A budget below the system prompt alone (a small context read at the clamp's top): the history goes, the system
    prompt and the question are what is sent, and the server says whether they fit. Before, this case returned the
    whole history untrimmed."""
    from harbor_clerk.llm import chat as chat_module

    messages = [{"role": "system", "content": "s" * 6000}]
    for i in range(4):
        messages += [{"role": "user", "content": f"q{i} " * 100}, {"role": "assistant", "content": f"a{i} " * 100}]
    messages.append({"role": "user", "content": "the question"})
    ctx = 1024
    schema = chat_module._get_tool_schema_tokens(None)
    assert ctx * chat_module._TRIM_BUDGET_FRACTION / chat_module._ESTIMATE_RATIO_MAX - schema < 6000 / 3.5, (
        "the scenario: the budget is below the system prompt alone"
    )
    trimmed = chat_module._trim_to_budget(messages, ctx, None, chat_module._ESTIMATE_RATIO_MAX)
    assert trimmed == [messages[0], messages[-1]], "the floor: system prompt and question, nothing else"
    assert chat_module._trim_to_budget(messages[:1] + messages[-1:], ctx, None, 3.0) == [messages[0], messages[-1]]


def test_the_history_trim_reads_the_estimate_at_the_ratio():
    """A history that fits the budget at the estimator's face value does not fit when the tokenizer counts twice
    the estimate: at that ratio the oldest exchanges go, and the system prompt and the question stay."""
    from harbor_clerk.llm import chat as chat_module

    messages = [{"role": "system", "content": "s" * 2000}]
    for i in range(6):
        messages += [{"role": "user", "content": f"q{i} " * 200}, {"role": "assistant", "content": f"a{i} " * 200}]
    messages.append({"role": "user", "content": "the question"})
    total = chat_module._estimate_messages_tokens(messages)
    schema = chat_module._get_tool_schema_tokens(None)
    fraction = chat_module._TRIM_BUDGET_FRACTION
    assert fraction < chat_module._TOOL_LOOP_BUDGET and fraction < 1 - chat_module._RESPONSE_RESERVE, (
        "the trim lands a first call under the budget stop, tools included, and under the reserve"
    )
    ctx = int((total + schema) * 1.5 / fraction)
    assert chat_module._trim_to_budget(messages, ctx, None, 1.0) == messages, "fits at face value"
    trimmed = chat_module._trim_to_budget(messages, ctx, None, 2.0)
    assert len(trimmed) < len(messages), "trimmed once the estimate is read at twice its face value"
    assert trimmed[0] == messages[0] and trimmed[-1] == messages[-1]
    assert (chat_module._estimate_messages_tokens(trimmed) + schema) * 2.0 <= ctx * fraction


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", [2.0, 3.0])
async def test_the_context_budget_stop_fires_at_the_servers_count_not_the_estimates(
    db_session, admin_user, chat_session_factory, monkeypatch, ratio
):
    """The server counts every prompt at ``ratio`` times the estimate and says so in each call's usage. The stop
    fires when the server's count of the next prompt reaches the budget, within a round's worth of it, where the
    estimate alone would have let the search run on past the context. The search-over message is the forced
    prompt's last line and both requests ask for usage."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 32768
    client, sent = _counting_client(ratio)
    table: dict = {}
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        budget=0.0,
        result=_big_search_result(12),
        context_tokens=ctx,
        ratios=table,
    )
    assert done["stop_reason"] == "context_budget" and text == "From the results."
    assert 2 <= len(tool_calls) < chat_module._MAX_TOOL_ROUNDS
    assert table[_MODEL_KEY] == pytest.approx(ratio, abs=0.05), "the server's ratio, measured on the tool rounds"
    assert all(p["stream_options"] == {"include_usage": True} for p in sent)
    forced = client.stream.call_args.kwargs["json"]
    assert forced["stream_options"] == {"include_usage": True}
    assert forced["messages"][-1] == {"role": "user", "content": chat_module._SEARCH_OVER_MESSAGE}

    threshold = chat_module._TOOL_LOOP_BUDGET
    stopped = forced["messages"][:-1]  # what the next round would have sent, with the tools
    counted_at_stop = ratio * chat_module._estimate_payload_tokens(stopped, sent[-1]["tools"])
    step = ratio * (
        chat_module._estimate_payload_tokens(stopped, sent[-1]["tools"])
        - chat_module._estimate_payload_tokens(sent[-1]["messages"], sent[-1]["tools"])
    )
    assert step < 0.1 * ctx, "the scenario: a round adds under a tenth of the context"
    assert threshold * ctx <= counted_at_stop < (threshold + 0.1) * ctx, (
        f"the stop fired at {counted_at_stop / ctx:.0%} of the context by the server's count"
    )
    # Not a round earlier: the round before was under the budget by the server's count, within the sliver by which
    # the ratio, still converging on the server's, under-read that round's increment.
    assert (
        ratio * chat_module._estimate_payload_tokens(sent[-1]["messages"], sent[-1]["tools"]) < (threshold + 0.01) * ctx
    ), "and not a round earlier"
    assert chat_module._estimate_payload_tokens(stopped, sent[-1]["tools"]) < threshold * ctx / 1.5, (
        "the estimate at face value was nowhere near the budget: the stop is the calibration's"
    )


@pytest.mark.asyncio
async def test_a_server_that_reports_no_usage_leaves_the_default_in_force(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """No usage in the stream (an older build, or a server that ignores the option): nothing is recorded, and the
    context percentage the UI gets is the estimate read at the default ratio."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 32768
    client, _ = _mock_client([[_chunk(content="Done."), "data: [DONE]"]])
    table: dict = {}
    text, done, _ = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=ctx, ratios=table
    )
    assert text == "Done." and table == {}
    messages = client.build_request.call_args.kwargs["json"]["messages"]
    estimate = chat_module._estimate_messages_tokens(messages) + chat_module._get_tool_schema_tokens(None)
    estimate += chat_module._estimate_tokens("Done.")
    assert done["context_pct"] == round(min(estimate * chat_module._ESTIMATE_RATIO_DEFAULT / ctx, 1.0) * 100)
    assert done["context_pct"] != round(min(estimate / ctx, 1.0) * 100), "the default is not face value"


@pytest.mark.asyncio
async def test_a_conversation_on_a_measured_model_starts_calibrated(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model was measured at three times the estimate in an earlier conversation of this process. A fresh
    conversation reads its estimate at that ratio from its first call, before the server has said anything."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 32768
    client, _ = _mock_client([[_chunk(content="Done."), "data: [DONE]"]])
    table = {_MODEL_KEY: 3.0}
    text, done, _ = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=ctx, ratios=table
    )
    assert text == "Done." and table == {_MODEL_KEY: 3.0}, "no usage came: the measurement stands"
    messages = client.build_request.call_args.kwargs["json"]["messages"]
    estimate = chat_module._estimate_messages_tokens(messages) + chat_module._get_tool_schema_tokens(None)
    estimate += chat_module._estimate_tokens("Done.")
    assert done["context_pct"] == round(min(estimate * 3.0 / ctx, 1.0) * 100)
    assert done["context_pct"] != round(min(estimate * chat_module._ESTIMATE_RATIO_DEFAULT / ctx, 1.0) * 100)


@pytest.mark.asyncio
async def test_the_clamp_holds_against_an_absurd_count(db_session, admin_user, chat_session_factory, monkeypatch):
    """A server whose count is a thousand times the estimate: its exact count is believed (the next prompt is
    predicted from it, so the search stops), but the ratio it implies moves the table half way to the clamp's top
    and no further, so the next conversation's trim and increments are read at a sane figure."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, sent = _counting_client(1000.0)
    table: dict = {}
    text, done, tool_calls = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=32768, ratios=table
    )
    assert tool_calls == ["search_documents"] and len(sent) == 1, "the count said the context was full"
    assert done["stop_reason"] == "context_budget" and text == "From the results."
    top, default = chat_module._ESTIMATE_RATIO_MAX, chat_module._ESTIMATE_RATIO_DEFAULT
    assert table[_MODEL_KEY] == pytest.approx((default + top) / 2), "the forced prompt's count, clamped, blended"
    assert table[_MODEL_KEY] <= top


@pytest.mark.asyncio
async def test_the_forced_calls_outer_deadline_uses_the_measured_ratio(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The server counted the loop's one call at three times the estimate. The forced call's first token arrives
    after the deadline the bare estimate at the default ratio would have set and before the one the prediction sets
    (that count, plus the increment at the ratio): it is the model's answer, not a cut, because the read allowance
    is for the prompt as the server counts it."""
    import asyncio

    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    clock = _Clock()
    client, sent = _counting_client(3.0)
    table: dict = {}

    async def first_token_between_the_deadlines():
        payload = client.stream.call_args.kwargs["json"]
        estimate = chat_module._estimate_messages_tokens(payload["messages"])
        default = chat_module._ESTIMATE_RATIO_DEFAULT
        assert table == {}, "the loop's call held no tool result: it is counted, not a measurement of the ratio"
        counted = int(3.0 * chat_module._estimate_payload_tokens(sent[0]["messages"], sent[0]["tools"]))
        appended = payload["messages"][len(sent[0]["messages"]) :]
        predicted = counted + int(default * chat_module._estimate_messages_tokens(appended))
        by_estimate = chat_module._forced_call_outer_deadline(301.0, int(default * estimate))
        by_prediction = chat_module._forced_call_outer_deadline(301.0, predicted)
        assert by_prediction > by_estimate + 2.0, "the scenario: the server's count buys a longer read"
        clock.now = by_estimate + 1.0
        yield _chunk(content="The fee was $4,500.")
        yield _finish_chunk("stop")
        yield "data: [DONE]"

    _forced_response(client, first_token_between_the_deadlines)
    text, done, tool_calls = await asyncio.wait_for(
        _drive(
            conv.conversation_id,
            admin_user,
            client,
            clock,
            monkeypatch,
            budget=300.0,
            on_tool=lambda: setattr(clock, "now", 301.0),
            ratios=table,
        ),
        timeout=10.0,
    )
    assert tool_calls == ["search_documents"] and done["stop_reason"] == "time_budget"
    assert text.startswith("The fee was $4,500.") and "I stopped after" in text, text


@pytest.mark.asyncio
async def test_a_refusals_count_is_recorded_too(db_session, admin_user, chat_session_factory, monkeypatch):
    """The server's refusal of the forced prompt names its exact count of it: the best measurement there is, and
    the next conversation on this model starts from it."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([[_tool_call_chunk(), "data: [DONE]"]])
    ctx = 8192
    counted: list[int] = []

    async def serve(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        if not counted:
            counted.append(3 * chat_module._estimate_messages_tokens(payload["messages"]))
            return _refusal(counted[0], ctx)
        return _lines_response([_chunk(content="From the results, $4,500."), "data: [DONE]"])

    client.stream.return_value.__aenter__ = AsyncMock(side_effect=serve)
    table: dict = {}
    monkeypatch.setattr(chat_module, "_estimate_ratios", table)
    text, done = await _drive_context_stop(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, context_tokens=ctx, budget=0.0
    )
    assert done["stop_reason"] == "context_budget" and text == "From the results, $4,500."
    assert client.stream.call_count == 2
    assert chat_module._estimate_ratios[_MODEL_KEY] == pytest.approx((chat_module._ESTIMATE_RATIO_DEFAULT + 3.0) / 2)


@pytest.mark.asyncio
async def test_the_forced_calls_count_blends_into_the_table(db_session, admin_user, chat_session_factory, monkeypatch):
    """The loop's calls measured the server at three times the estimate; the forced call, a different prompt (no
    tools, the search-over message), is counted at twice. Its usage chunk, choices empty, is read like the loop's:
    the ratio blends half way, to 2.5."""
    conv = await _conversation(db_session, admin_user)
    client, sent = _counting_client(3.0, forced_ratio=2.0)
    table: dict = {}
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        budget=0.0,
        result=_big_search_result(12),
        context_tokens=32768,
        ratios=table,
    )
    assert done["stop_reason"] == "context_budget" and text == "From the results." and len(tool_calls) >= 2
    assert table[_MODEL_KEY] == pytest.approx(2.5, abs=0.05), "about 3.0 from the loop, half way to the forced 2.0"


@pytest.mark.asyncio
async def test_tokens_used_is_the_last_prompt_plus_every_completion(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The stored row's tokens_used: the forced call's prompt (the last one sent, which holds the whole conversation)
    plus the completion tokens of every call, the loop's rounds and the forced call. Not a running total of prompts,
    which counted the history once per round."""
    from sqlalchemy import select

    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    client, sent = _counting_client(2.0)
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        budget=0.0,
        result=_big_search_result(12),
        context_tokens=32768,
    )
    assert done["stop_reason"] == "context_budget" and len(sent) >= 2
    forced = client.stream.call_args.kwargs["json"]["messages"]
    last_prompt = int(2.0 * chat_module._estimate_messages_tokens(forced))
    rows = await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id))
    stored = [r for r in rows.scalars() if r.role == "assistant" and r.content][-1]
    assert stored.tokens_used == last_prompt + 5 * (len(sent) + 1), (
        f"{stored.tokens_used} is not the last prompt ({last_prompt}) plus {len(sent) + 1} completions of 5"
    )
    assert stored.tokens_used < 2 * 32768, "not several times the context"


@pytest.mark.asyncio
async def test_a_finished_answers_trailing_chunks_past_the_grace_cut_nothing(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The answer's last token came inside the grace; the finish chunk, a blank separator, the usage chunk and
    "[DONE]" arrive after the grace ran out. None of them carries a token, so nothing is cut: the answer stands as
    the model's own, with no stop reason and no note."""
    conv = await _conversation(db_session, admin_user)
    clock = _Clock()

    def move(n, i):
        if n == 0 and i == 0:
            clock.now = 301.0  # past the deadline, inside the grace
        if n == 0 and i == 1:
            clock.now = 301.0 + 121.0  # the grace is gone before the trailing chunks arrive

    client, _ = _mock_client(
        [[_chunk(content="The fee was $4,500."), _finish_chunk("stop"), "", _usage_chunk(900), "data: [DONE]"]],
        on_line=move,
    )
    text, done, tool_calls = await _drive(conv.conversation_id, admin_user, client, clock, monkeypatch, budget=300.0)
    assert text == "The fee was $4,500.", text
    assert "stop_reason" not in done and "I stopped after" not in text
    assert client.stream.call_count == 0, "nothing was forced"


def test_the_lines_that_carry_no_token():
    from harbor_clerk.llm import chat as chat_module

    assert chat_module._line_carries_no_token("data: [DONE]")
    assert chat_module._line_carries_no_token("")
    assert chat_module._line_carries_no_token(": keepalive")
    assert chat_module._line_carries_no_token("data: not json")
    assert chat_module._line_carries_no_token(_usage_chunk(900))
    assert chat_module._line_carries_no_token(_finish_chunk("stop"))
    assert not chat_module._line_carries_no_token(_chunk(content="x"))
    assert not chat_module._line_carries_no_token(_chunk(reasoning_content="hmm"))
    assert not chat_module._line_carries_no_token(_tool_call_chunk())


@pytest.mark.asyncio
async def test_tokens_used_without_a_forced_call_is_the_loops_last_prompt(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The model answers on its own after two tool rounds: the stored tokens_used is the third call's prompt (the
    last sent, with the tools) plus three completions, not the three prompts summed."""
    from sqlalchemy import select

    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    client, sent = _counting_client(2.0, rounds=2)
    text, done, tool_calls = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=32768
    )
    assert text == "Answered on my own." and "stop_reason" not in done and len(sent) == 3
    last_prompt = int(2.0 * chat_module._estimate_payload_tokens(sent[-1]["messages"], sent[-1]["tools"]))
    rows = await db_session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id))
    stored = [r for r in rows.scalars() if r.role == "assistant" and r.content][-1]
    assert stored.tokens_used == last_prompt + 5 * 3, f"{stored.tokens_used} != {last_prompt} + 15"
    assert done["context_pct"] == round(min((last_prompt + 5) / 32768, 1.0) * 100), (
        "the UI percentage is the server's count of the last prompt and its completion, not the estimate"
    )


def _two_density_client(ctx: int, without_tools: float = 1.2, with_tools: float = 2.2, rounds: int | None = None):
    """The second review's server: a prompt with no tool result tokenizes at ``without_tools`` times the estimate (the
    system prompt, the schema and the question are prose), one with a tool result at ``with_tools``. The model
    calls a tool every round (the first ``rounds``, when given, then answers). A prompt counted over ``ctx`` is
    refused with the pin's 400, as a real server would, and the forced call likewise. Returns the client, the loop's
    payloads and the server's count of each."""
    from harbor_clerk.llm import chat as chat_module

    sent: list[dict] = []
    counts: list[int] = []
    forced_counts: list[int] = []

    def density(messages: list[dict]) -> float:
        return with_tools if any(m.get("role") == "tool" for m in messages) else without_tools

    def build(*_a, **_k):
        payload = client.build_request.call_args.kwargs["json"]
        sent.append({**payload, "messages": list(payload["messages"])})
        estimate = chat_module._estimate_payload_tokens(payload["messages"], payload.get("tools"))
        counted = int(density(payload["messages"]) * estimate)
        counts.append(counted)
        if counted > ctx:
            return _refusal(counted, ctx)
        if rounds is not None and len(sent) > rounds:
            return _lines_response([_chunk(content="Answered on my own."), _usage_chunk(counted), "data: [DONE]"])
        return _lines_response([_tool_call_chunk(), _usage_chunk(counted), "data: [DONE]"])

    def forced(*_a, **_k):
        payload = client.stream.call_args.kwargs["json"]
        counted = int(density(payload["messages"]) * chat_module._estimate_messages_tokens(payload["messages"]))
        forced_counts.append(counted)
        if counted > ctx:
            return _refusal(counted, ctx)
        return _lines_response(
            [_chunk(content="From the results."), _finish_chunk("stop"), _usage_chunk(counted), "data: [DONE]"]
        )

    client = MagicMock()
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(side_effect=build)
    client.aclose = AsyncMock()
    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(side_effect=forced)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_ctx)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, sent, counts, forced_counts


@pytest.mark.asyncio
async def test_the_first_tool_less_call_does_not_calibrate_the_results_that_follow(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The second review's run: a 16,384 slot, tool-less prompts at 1.2x, tool-bearing at 2.2x, a result at the cap
    each round, a fresh table. The first call is counted at 1.2x. A ratio taken from it read the first at-cap result
    at 1.2x and the next prompt went out at 106% of the slot, refused with the hard error. The next prompt is now
    predicted from the first call's exact count plus the result at the default ratio, so the search stops before
    the prompt the server would refuse, and the forced prompt (no tools) fits."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 16384
    client, sent, counts, forced_counts = _two_density_client(ctx)
    table: dict = {}
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        budget=0.0,
        result=_big_search_result(60),
        context_tokens=ctx,
        ratios=table,
    )
    assert done["stop_reason"] == "context_budget", done
    assert text == "From the results.", text
    assert tool_calls == ["search_documents"] and len(sent) == 1, "the search stopped before the second prompt"
    assert counts[0] < 0.5 * ctx, "the first prompt, prose at 1.2x, was far under the budget"
    forced = client.stream.call_args.kwargs["json"]["messages"]
    stopped = forced[:-1]  # what the second round would have sent, with the tools
    would_have_counted = 2.2 * chat_module._estimate_payload_tokens(stopped, sent[0]["tools"])
    assert would_have_counted > ctx, "the scenario: the second prompt would have been refused"
    assert all(c <= ctx for c in counts), "no loop prompt the server would refuse was sent"
    assert forced_counts == [c for c in forced_counts if c <= ctx] and len(forced_counts) == 1, (
        "the forced prompt fit the slot at the server's count, first time"
    )
    assert forced_counts[0] < ctx, f"the forced prompt counted {forced_counts[0]} of {ctx}"
    threshold = chat_module._TOOL_LOOP_BUDGET
    assert counts[-1] < threshold * ctx, "the last prompt sent was under the budget by the server's count"
    default = chat_module._ESTIMATE_RATIO_DEFAULT
    assert table[_MODEL_KEY] == pytest.approx((default + 2.2) / 2, abs=0.01), (  # the fake counts in whole tokens
        "only the forced prompt, which held a result, was measured (2.2x blended from the default); the first call, "
        "counted at 1.2x, was not, or the ratio would stand below the default"
    )


@pytest.mark.asyncio
async def test_a_tool_heavy_conversation_after_prose_ones_starts_from_the_default(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """Prose conversations on the model (the model answers without a tool) are counted at 1.2x and record nothing:
    the table is untouched, and the tool-heavy conversation that follows starts from the default."""
    from harbor_clerk.llm import chat as chat_module

    table: dict = {}
    for _ in range(3):
        conv = await _conversation(db_session, admin_user)
        client, sent = _counting_client(1.2, rounds=0)
        text, done, tool_calls = await _drive(
            conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, ratios=table
        )
        assert text == "Answered on my own." and tool_calls == [] and len(sent) == 1
    assert table == {}, "three prose conversations, counted, recorded nothing"
    assert chat_module._estimate_ratio(_MODEL_KEY) == chat_module._ESTIMATE_RATIO_DEFAULT


@pytest.mark.asyncio
async def test_a_trimmed_first_call_carries_the_tools(db_session, admin_user, chat_session_factory, monkeypatch):
    """Thirty prose exchanges on a 16,384 slot: the history is trimmed, and the first call still carries the tool
    definitions and is predicted under the budget stop. Trimmed to the reserve alone it landed in the band where the
    tools were omitted, and the model, told to search first, answered from the history."""
    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    for i in range(30):
        db_session.add(ChatMessage(conversation_id=conv.conversation_id, role="user", content=f"Question {i}? " * 40))
        db_session.add(
            ChatMessage(conversation_id=conv.conversation_id, role="assistant", content=f"Answer {i}. " * 50)
        )
    await db_session.commit()
    ctx = 16384
    client, _ = _mock_client([[_chunk(content="Done."), "data: [DONE]"]])
    text, done, _ = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=ctx
    )
    assert text == "Done."
    payload = client.build_request.call_args.kwargs["json"]
    history = get_settings().max_history_messages
    assert len(payload["messages"]) < history + 1, "the history was trimmed"
    assert payload.get("tools"), "and the first call carries the tools"
    predicted = chat_module._estimate_payload_tokens(payload["messages"], payload["tools"])
    predicted *= chat_module._ESTIMATE_RATIO_DEFAULT
    assert predicted <= chat_module._TOOL_LOOP_BUDGET * ctx, "under the budget stop with the tools in"
    assert predicted <= chat_module._TRIM_BUDGET_FRACTION * ctx


@pytest.mark.asyncio
async def test_the_prediction_reads_the_prose_at_the_servers_count_and_only_the_result_at_the_ratio(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """The other side of the prediction: the first prompt is prose the server counts at 1.2x, and the one result is
    mid-sized. The whole estimate at the default ratio puts the second prompt over the budget and would stop the
    search; predicted from the server's count of the first prompt plus the result at the ratio it is well under, so
    the second call goes out, is counted inside the slot, and the model answers on its own."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 16384
    client, sent, counts, _forced = _two_density_client(ctx, rounds=1)
    text, done, tool_calls = await _drive(
        conv.conversation_id,
        admin_user,
        client,
        _Clock(),
        monkeypatch,
        budget=0.0,
        result=_big_search_result(44),
        context_tokens=ctx,
    )
    assert tool_calls == ["search_documents"] and len(sent) == 2, "the second call went out"
    assert text == "Answered on my own." and "stop_reason" not in done
    threshold, default = chat_module._TOOL_LOOP_BUDGET, chat_module._ESTIMATE_RATIO_DEFAULT
    whole_estimate = default * chat_module._estimate_payload_tokens(sent[1]["messages"], sent[1]["tools"])
    assert whole_estimate >= threshold * ctx, "the scenario: the whole estimate at the ratio would have stopped here"
    assert counts[1] < ctx, "the server counted the second prompt inside the slot"
    predicted = counts[0] + default * chat_module._estimate_messages_tokens(
        sent[1]["messages"][len(sent[0]["messages"]) :]
    )
    assert predicted < threshold * ctx, "the prediction kept the search going"


@pytest.mark.asyncio
async def test_the_ui_percentage_is_the_servers_count_when_usage_came(
    db_session, admin_user, chat_session_factory, monkeypatch
):
    """A prose answer the server counts at 1.2x the estimate, with the default ratio (2.0) in force since nothing
    was recorded: the context percentage sent to the UI is the server's count plus the completion, not the
    estimate at the ratio, which would show nearly twice the usage."""
    from harbor_clerk.llm import chat as chat_module

    conv = await _conversation(db_session, admin_user)
    ctx = 32768
    client, sent = _counting_client(1.2, rounds=0)
    table: dict = {}
    text, done, _ = await _drive(
        conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0, context_tokens=ctx, ratios=table
    )
    assert text == "Answered on my own." and len(sent) == 1 and table == {}
    estimate = chat_module._estimate_payload_tokens(sent[0]["messages"], sent[0]["tools"])
    counted = int(1.2 * estimate)
    assert done["context_pct"] == round(min((counted + 5) / ctx, 1.0) * 100)
    by_estimate = (estimate + chat_module._estimate_tokens(text)) * chat_module._ESTIMATE_RATIO_DEFAULT
    assert done["context_pct"] != round(min(by_estimate / ctx, 1.0) * 100), "and not the estimate at the ratio"
