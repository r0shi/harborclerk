"""Verify research engine loads research_state.scope and threads it to execute_tool.

Approach mirrors test_chat_engine_scope_threading.py:
- Unit tests for build_user_scope are already covered there; we add a couple
  research-specific ones here for completeness.
- Wiring integration tests drive research_stream with a mocked LLM and
  mocked execute_tool to confirm user_scope is threaded through correctly.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harbor_clerk.api.scope import UserScope, build_user_scope

# ---------------------------------------------------------------------------
# Unit tests — build_user_scope is shared; verify it behaves the same when
# called from the research path (no branching in the helper itself, but
# belt-and-suspenders for the integration).
# ---------------------------------------------------------------------------


def test_build_user_scope_from_research_state_scope():
    """scope dict with folder_ids → UserScope with parsed UUIDs."""
    fid = uuid.uuid4()
    result = build_user_scope({"folder_ids": [str(fid)]})
    assert isinstance(result, UserScope)
    assert result.folder_ids == [fid]


def test_build_user_scope_none_returns_none():
    """state.scope=None → no restriction → None."""
    assert build_user_scope(None) is None


def test_build_user_scope_empty_folder_ids_returns_none():
    """state.scope={"folder_ids": []} → no restriction → None."""
    assert build_user_scope({"folder_ids": []}) is None


# ---------------------------------------------------------------------------
# Fixtures shared by wiring integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def mock_research_session_factory(db_session, _engine, monkeypatch):
    """Patch async_session_factory everywhere research_stream touches it."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as session:
            yield session

    monkeypatch.setattr("harbor_clerk.llm.research.async_session_factory", _factory)
    monkeypatch.setattr("harbor_clerk.topics.async_session_factory", _factory)


@pytest.fixture
async def scoped_research_state(db_session, admin_user, two_folder_corpus):
    """Conversation + ResearchState with scope restricted to folder_a."""
    from harbor_clerk.models.chat_message import ChatMessage
    from harbor_clerk.models.conversation import Conversation
    from harbor_clerk.models.research_state import ResearchState

    folder_a, _, _, _ = two_folder_corpus

    conv = Conversation(
        user_id=admin_user.user_id,
        title="Scoped research",
        mode="research",
    )
    db_session.add(conv)
    await db_session.flush()

    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy="search",
        status="queued",
        max_rounds=2,
        scope={"folder_ids": [str(folder_a.folder_id)]},
    )
    db_session.add(state)
    await db_session.flush()

    msg = ChatMessage(
        conversation_id=conv.conversation_id,
        role="user",
        content="What are the main findings?",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(conv)
    await db_session.refresh(state)

    return conv, state, folder_a


@pytest.fixture
async def unscoped_research_state(db_session, admin_user):
    """Conversation + ResearchState with no scope."""
    from harbor_clerk.models.chat_message import ChatMessage
    from harbor_clerk.models.conversation import Conversation
    from harbor_clerk.models.research_state import ResearchState

    conv = Conversation(
        user_id=admin_user.user_id,
        title="Unscoped research",
        mode="research",
    )
    db_session.add(conv)
    await db_session.flush()

    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy="search",
        status="queued",
        max_rounds=2,
    )
    db_session.add(state)
    await db_session.flush()

    msg = ChatMessage(
        conversation_id=conv.conversation_id,
        role="user",
        content="Tell me about everything.",
    )
    db_session.add(msg)
    await db_session.commit()

    return conv, state


# ---------------------------------------------------------------------------
# LLM mock helpers
# ---------------------------------------------------------------------------


def _make_planning_response(queries: list[str]) -> str:
    """Return a JSON string that looks like a query-planning LLM response."""
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"queries": queries}),
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )


def _make_note_extraction_response(notes: str = "Test notes.") -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": notes,
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )


def _make_synthesis_stream_response(text: str = "Final answer.") -> list[str]:
    chunk = json.dumps({"choices": [{"delta": {"content": text}}]})
    return [f"data: {chunk}", "data: [DONE]"]


def _make_mock_httpx_client(
    planning_resp: str,
    note_resp: str,
    synthesis_lines: list[str],
) -> MagicMock:
    """Return a mock AsyncClient that serves planning → note → synthesis."""
    call_order = [0]

    def make_non_streaming(body: str):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value=json.loads(body))
        resp.raise_for_status = MagicMock()
        return resp

    def make_streaming(lines: list[str]):
        resp = MagicMock()
        resp.status_code = 200

        async def aiter_lines():
            for line in lines:
                yield line

        resp.aiter_lines = aiter_lines
        resp.aclose = AsyncMock()
        return resp

    def _post(*args, **kwargs):
        call_order[0] += 1
        if call_order[0] == 1:
            # Phase 1: query planning (non-streaming)
            return make_non_streaming(planning_resp)
        elif call_order[0] == 2:
            # Phase 4: note extraction (non-streaming)
            return make_non_streaming(note_resp)
        else:
            # Synthesis (streaming)
            return make_streaming(synthesis_lines)

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    client.aclose = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Wiring integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_stream_passes_user_scope_to_execute_tool(
    db_session,
    admin_user,
    two_folder_corpus,
    scoped_research_state,
    mock_research_session_factory,
):
    """When research_state.scope has folder_ids, all execute_tool calls get
    a matching UserScope with those folder IDs."""
    from harbor_clerk.llm import research as research_module

    conv, state, folder_a = scoped_research_state

    captured_scopes: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, mode=None, user_scope=None):
        captured_scopes.append(user_scope)
        if fn_name == "batch_search":
            return json.dumps({"results": [{"query": fn_args.get("queries", ["q"])[0], "hits": []}]})
        if fn_name == "search_documents":
            return json.dumps({"hits": [], "has_more": False})
        if fn_name == "read_passages":
            return json.dumps({"passages": []})
        return json.dumps({})

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["test query"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response(),
    )

    with (
        patch.object(research_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        events = []
        async for event in research_module.research_stream(
            conv.conversation_id,
            user_id=admin_user.user_id,
        ):
            events.append(event)

    assert len(captured_scopes) >= 1, "execute_tool was never called"
    for i, scope in enumerate(captured_scopes):
        assert isinstance(scope, UserScope), f"execute_tool call #{i + 1} received {type(scope)} instead of UserScope"
        assert scope.folder_ids == [folder_a.folder_id], (
            f"execute_tool call #{i + 1}: expected folder_ids=[{folder_a.folder_id}], got {scope.folder_ids}"
        )


@pytest.mark.asyncio
async def test_research_stream_unscoped_passes_none_scope(
    db_session,
    admin_user,
    unscoped_research_state,
    mock_research_session_factory,
):
    """When research_state has no scope, execute_tool receives user_scope=None."""
    from harbor_clerk.llm import research as research_module

    conv, state = unscoped_research_state

    captured_scopes: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, mode=None, user_scope=None):
        captured_scopes.append(user_scope)
        if fn_name == "batch_search":
            return json.dumps({"results": [{"query": fn_args.get("queries", ["q"])[0], "hits": []}]})
        if fn_name == "search_documents":
            return json.dumps({"hits": [], "has_more": False})
        if fn_name == "read_passages":
            return json.dumps({"passages": []})
        return json.dumps({})

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["another query"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response("All done."),
    )

    with (
        patch.object(research_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        async for _ in research_module.research_stream(
            conv.conversation_id,
            user_id=admin_user.user_id,
        ):
            pass

    assert len(captured_scopes) >= 1, "execute_tool was never called"
    for i, scope in enumerate(captured_scopes):
        assert scope is None, f"execute_tool call #{i + 1}: expected None for unscoped run, got {scope!r}"
