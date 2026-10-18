"""Under Docker Compose, Ask and Research say the local model is starting, not "activate a model" (#654).

The model counts as active as soon as its file is in the volume; llama-server starts a few seconds later and
then loads. A request in that gap is refused, or answered 503, and the advice the macOS app gives for that,
"LLM server is not running. Select and activate a model in Settings.", is wrong there: the model is active.
Only a refused connection or a 503 reads as starting: a read timeout or a connection cut mid-answer is
something else, and keeps the old text.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.llm import research as research_module
from harbor_clerk.llm.health import MODEL_STARTING_MESSAGE, server_unready_message
from tests.test_chat_time_budget import (  # noqa: F401 — the fixture, requested by name
    _Clock,
    _conversation,
    _drive,
    _mock_client,
    chat_session_factory,
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

NOT_RUNNING = "LLM server is not running. Select and activate a model in Settings."
REQUEST = httpx.Request("POST", "http://llama-server:8080/v1/chat/completions")


def _loading() -> httpx.Response:
    return httpx.Response(503, text='{"error":{"message":"Loading model","code":503}}', request=REQUEST)


@pytest.fixture
def compose(monkeypatch):
    """This process is a Compose service: the marker docker-compose.yml sets, no native config."""
    monkeypatch.setattr(get_settings(), "hc_deployment", "compose")
    monkeypatch.setattr(get_settings(), "native_config_file", "")


async def _ask(db_session, admin_user, monkeypatch, send: AsyncMock) -> list[str]:
    """One chat turn against a llama-server that `send` answers; the stored assistant messages."""
    from harbor_clerk.models.chat_message import ChatMessage

    conv = await _conversation(db_session, admin_user)
    client, _ = _mock_client([["data: [DONE]"]])
    client.send = send
    _, done, _ = await _drive(conv.conversation_id, admin_user, client, _Clock(), monkeypatch, budget=0.0)
    assert done["type"] == "done"
    rows = await db_session.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == conv.conversation_id, ChatMessage.role == "assistant")
    )
    return [m.content for m in rows.scalars().all()]


async def _research(conv, admin_user, send: AsyncMock) -> list[dict]:
    """One research run whose synthesis `send` answers; its error events."""
    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["termination notice period"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response(),
    )
    mock_client.send = send  # synthesis streams through send
    with (
        patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool()),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]
    return [e for e in _events(raw) if e.get("type") == "error"]


# --- Ask ---


@pytest.mark.asyncio
async def test_ask_says_the_model_is_starting_when_llama_server_refuses(
    db_session,
    admin_user,
    chat_session_factory,  # noqa: F811
    monkeypatch,
    compose,
):
    send = AsyncMock(side_effect=httpx.ConnectError("refused"))
    assert await _ask(db_session, admin_user, monkeypatch, send) == [f"Error: {MODEL_STARTING_MESSAGE}"]


@pytest.mark.asyncio
async def test_ask_says_the_model_is_starting_when_llama_server_is_loading(
    db_session,
    admin_user,
    chat_session_factory,  # noqa: F811
    monkeypatch,
    compose,
):
    send = AsyncMock(side_effect=lambda *a, **k: _loading())  # a 503 on the try and on its one retry
    stored = await _ask(db_session, admin_user, monkeypatch, send)
    assert len(stored) == 1 and stored[0].startswith(f"Error: {MODEL_STARTING_MESSAGE}"), stored


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx.ReadTimeout("no answer in time"), id="read-timeout"),
        pytest.param(httpx.RemoteProtocolError("peer closed connection"), id="cut"),
    ],
)
@pytest.mark.asyncio
async def test_ask_keeps_its_advice_for_other_transport_errors(
    db_session,
    admin_user,
    chat_session_factory,  # noqa: F811
    monkeypatch,
    compose,
    exc,
):
    stored = await _ask(db_session, admin_user, monkeypatch, AsyncMock(side_effect=exc))
    assert stored == [f"Error: {NOT_RUNNING}"]


@pytest.mark.asyncio
async def test_outside_compose_ask_keeps_its_advice(
    db_session,
    admin_user,
    chat_session_factory,  # noqa: F811
    monkeypatch,
):
    stored = await _ask(db_session, admin_user, monkeypatch, AsyncMock(side_effect=httpx.ConnectError("refused")))
    assert stored == [f"Error: {NOT_RUNNING}"]


# --- Research ---


@pytest.mark.asyncio
async def test_research_says_the_model_is_starting_when_synthesis_is_refused(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
    compose,
):
    conv, _ = unscoped_research_state
    errors = await _research(conv, admin_user, AsyncMock(side_effect=httpx.ConnectError("refused")))
    assert errors and errors[-1]["message"] == MODEL_STARTING_MESSAGE, errors


@pytest.mark.asyncio
async def test_research_says_the_model_is_starting_when_synthesis_is_loading(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
    compose,
):
    conv, _ = unscoped_research_state
    errors = await _research(conv, admin_user, AsyncMock(side_effect=lambda *a, **k: _loading()))
    assert errors and errors[-1]["message"] == MODEL_STARTING_MESSAGE, errors


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx.ReadTimeout("no answer in time"), id="read-timeout"),
        pytest.param(httpx.RemoteProtocolError("peer closed connection"), id="cut"),
    ],
)
@pytest.mark.asyncio
async def test_research_keeps_its_advice_for_other_transport_errors(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
    compose,
    exc,
):
    conv, _ = unscoped_research_state
    errors = await _research(conv, admin_user, AsyncMock(side_effect=exc))
    assert errors and errors[-1]["message"] == NOT_RUNNING, errors


# --- the rule ---


def test_only_a_refusal_or_a_503_reads_as_starting(compose) -> None:
    assert server_unready_message("raw", exc=httpx.ConnectError("refused")) == MODEL_STARTING_MESSAGE
    assert server_unready_message("LLM error (503)", status_code=503) == MODEL_STARTING_MESSAGE
    assert server_unready_message("LLM error (500)", status_code=500) == "LLM error (500)"
    assert server_unready_message("raw", exc=httpx.ReadTimeout("slow")) == "raw"
    assert server_unready_message("raw", exc=httpx.ConnectTimeout("nothing")) == "raw"
