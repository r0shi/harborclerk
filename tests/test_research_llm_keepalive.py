"""A research LLM call in flight is not a stall: the stream heartbeats and reports while it waits (#720).

Note extraction is one non-streaming call. gpt-oss-20b on the mini wrote 5,200-token notes at 16 tokens a
second, so the call ran over five minutes with no heartbeat and no SSE event; the API's reaper marked the task
stale at five minutes and the eval harness aborted at 325 s of silence, both while the model was working. The
180 s timeout then fired and its retry started the same generation again behind the first, on the single slot.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from harbor_clerk.llm import research as research_module
from harbor_clerk.llm.research import (
    _MAX_TOKENS_NOTES,
    _SLOW_LLM_TIMEOUT,
    _check_gaps,
    _extract_notes,
    _llm_complete,
    _with_keepalive,
)
from tests.test_research_engine_scope_threading import (  # noqa: F401 — fixtures
    _make_mock_httpx_client,
    _make_note_extraction_response,
    _make_planning_response,
    _make_synthesis_stream_response,
    mock_research_session_factory,
    unscoped_research_state,
)
from tests.test_research_fan_out_progress import _events, _fake_execute_tool

# ---------------------------------------------------------------------------
# _with_keepalive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_keepalive_ticks_while_a_coroutine_runs_then_yields_its_result_once():
    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "notes"

    steps = [s async for s in _with_keepalive(slow(), interval=0.02)]

    assert steps[-1] == ("item", "notes")
    assert steps.count(("item", "notes")) == 1
    assert steps.count(("keepalive", None)) >= 1, steps


@pytest.mark.asyncio
async def test_with_keepalive_yields_a_fast_coroutine_without_a_tick():
    async def fast() -> int:
        return 7

    assert [s async for s in _with_keepalive(fast(), interval=1.0)] == [("item", 7)]


@pytest.mark.asyncio
async def test_with_keepalive_passes_an_iterators_items_through_in_order_with_ticks_in_the_gaps():
    async def tokens():
        yield "a"
        await asyncio.sleep(0.1)
        yield "b"

    steps = [s async for s in _with_keepalive(tokens(), interval=0.02)]

    assert [v for k, v in steps if k == "item"] == ["a", "b"]
    assert steps[0] == ("item", "a") and steps[-1] == ("item", "b")
    assert ("keepalive", None) in steps[1:-1], steps


@pytest.mark.asyncio
async def test_with_keepalive_raises_the_sources_exception_after_the_items_before_it():
    async def failing():
        yield "partial"
        raise httpx.ReadTimeout("model stalled")

    seen = []
    with pytest.raises(httpx.ReadTimeout):
        async for step in _with_keepalive(failing(), interval=1.0):
            seen.append(step)
    assert seen == [("item", "partial")]


@pytest.mark.asyncio
async def test_with_keepalive_cancels_the_source_when_closed_early():
    """A client that disconnects mid-call must not leave the LLM request running."""
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    gen = _with_keepalive(forever(), interval=0.02)
    assert await anext(gen) == ("keepalive", None)
    await gen.aclose()

    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# The timeout shape of the non-streaming call
# ---------------------------------------------------------------------------


def _client_that(*side_effects):
    client = MagicMock()
    client.post = AsyncMock(side_effect=list(side_effects))
    return client


def _ok(content: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": content}}], "usage": {}})
    return resp


@pytest.mark.asyncio
async def test_llm_complete_does_not_retry_a_read_timeout():
    """The model was still generating when the deadline passed; a second request would queue behind the first
    on the single slot and double the wait. The caller decides the fallback."""
    client = _client_that(httpx.ReadTimeout("still generating"), _ok("too late"))

    with patch("harbor_clerk.llm.research.asyncio.sleep", new=AsyncMock()), pytest.raises(httpx.ReadTimeout):
        await _llm_complete(client, "http://llm/v1/chat/completions", [], phase="notes")

    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_llm_complete_still_retries_once_when_the_server_is_unreachable():
    client = _client_that(httpx.ConnectError("refused"), _ok("planned"))

    with patch("harbor_clerk.llm.research.asyncio.sleep", new=AsyncMock()):
        assert await _llm_complete(client, "http://llm/v1/chat/completions", [], phase="planning") == "planned"

    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_note_extraction_and_gap_analysis_get_the_slow_call_timeout():
    """Sized for the notes cap at the slowest observed rate (16 tokens a second, #720), plus prefill; a
    timeout that fires on a working call is the failure this guards against."""
    assert _SLOW_LLM_TIMEOUT >= _MAX_TOKENS_NOTES / 16 + 60

    client = _client_that(_ok("notes"), _ok('{"gaps": []}'))
    await _extract_notes(client, "http://llm", "q", "passage text")
    await _check_gaps(client, "http://llm", "q", "notes", "coverage")

    assert [call.kwargs["timeout"] for call in client.post.await_args_list] == [_SLOW_LLM_TIMEOUT] * 2


# ---------------------------------------------------------------------------
# The stream, end to end
# ---------------------------------------------------------------------------


async def _heartbeat_of(conversation_id):
    from harbor_clerk.models.research_state import ResearchState

    async with research_module.async_session_factory() as session:
        return (await session.get(ResearchState, conversation_id)).heartbeat_at


@pytest.mark.asyncio
async def test_research_stream_heartbeats_and_reports_while_note_extraction_runs(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
    monkeypatch,
):
    """During a note-extraction call that outlives the keepalive interval, the persisted heartbeat must move
    and the stream must emit progress events naming the call; afterwards the run must finish with ``done``."""
    conv, _state = unscoped_research_state
    monkeypatch.setattr(research_module, "_LLM_KEEPALIVE_INTERVAL", 0.02)
    heartbeats: dict[str, object] = {}

    async def slow_notes(client, url, question, passages, coverage) -> str:
        heartbeats["at_call_start"] = await _heartbeat_of(conv.conversation_id)
        await asyncio.sleep(0.15)
        heartbeats["at_call_end"] = await _heartbeat_of(conv.conversation_id)
        return "Notes from a slow model."

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["termination notice period clause"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response("Final answer."),
    )
    with (
        patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool()),
        patch.object(research_module, "_extract_notes_with_retry", new=slow_notes),
        patch.object(research_module, "_check_gaps", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]

    assert heartbeats["at_call_end"] > heartbeats["at_call_start"], heartbeats

    events = _events(raw)
    waiting = [i for i, e in enumerate(events) if e.get("type") == "progress" and e.get("llm_call") == "notes"]
    assert waiting, [e for e in events if e.get("type") == "progress"]
    assert all(
        events[i]["step"] == 4 and events[i]["phase"] == "analyzing" and events[i]["llm_call_seconds"] >= 0
        for i in waiting
    )
    notes_index = next(i for i, e in enumerate(events) if e.get("type") == "notes" and "slow model" in e["content"])
    assert max(waiting) < notes_index, "a keepalive was sent after the call returned"
    assert events[-1]["type"] == "done", events[-1]
    assert "".join(e["content"] for e in events if e.get("type") == "token") == "Final answer."


@pytest.mark.asyncio
async def test_research_stream_heartbeats_while_synthesis_is_silent(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
    monkeypatch,
):
    """A model that routes its answer through the reasoning channel sends no token for the whole synthesis;
    the stream must still heartbeat and report, and the tokens that do arrive must be relayed as before."""
    conv, _state = unscoped_research_state
    monkeypatch.setattr(research_module, "_LLM_KEEPALIVE_INTERVAL", 0.02)
    heartbeats: dict[str, object] = {}

    async def silent_then_answer(client, url, messages, *, timeout=None, max_tokens=None):
        heartbeats["at_call_start"] = await _heartbeat_of(conv.conversation_id)
        await asyncio.sleep(0.15)
        heartbeats["at_call_end"] = await _heartbeat_of(conv.conversation_id)
        yield "Final "
        yield "answer."

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["termination notice period clause"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=[],
    )
    with (
        patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(hits=False)),
        patch.object(research_module, "_stream_llm_tokens", new=silent_then_answer),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]

    assert heartbeats["at_call_end"] > heartbeats["at_call_start"], heartbeats
    events = _events(raw)
    assert any(e.get("type") == "progress" and e.get("llm_call") == "synthesis" for e in events)
    assert "".join(e["content"] for e in events if e.get("type") == "token") == "Final answer."
    assert events[-1]["type"] == "done", events[-1]


@pytest.mark.asyncio
async def test_research_stream_falls_back_to_raw_passages_when_note_extraction_times_out(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
):
    """A read timeout raised inside the keepalive loop reaches the phase's own handler: raw passages go to
    synthesis and the run still completes."""
    conv, _state = unscoped_research_state

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["termination notice period clause"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response("Final answer."),
    )
    with (
        patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(hits=False)),
        patch.object(
            research_module, "_extract_notes_with_retry", new=AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        ),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]

    events = _events(raw)
    assert any(e.get("type") == "notes" and e["content"].startswith("## Raw passages") for e in events)
    assert events[-1]["type"] == "done", events[-1]
