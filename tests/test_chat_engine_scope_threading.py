"""Verify chat engine loads conversation.scope and passes it to execute_tool.

Approach: Option A — unit-test the small build_user_scope helper directly
(avoids SQLAlchemy ORM instrumentation overhead),
plus wiring integration tests that drive chat_stream with a mocked LLM and
mocked execute_tool to confirm user_scope is threaded through correctly.
"""

import uuid

import pytest

from harbor_clerk.api.scope import UserScope, build_user_scope

# ---------------------------------------------------------------------------
# Unit tests for build_user_scope
# Passes raw JSONB dicts — no SQLAlchemy instrumentation involved.
# ---------------------------------------------------------------------------


def test_build_user_scope_empty_dict_returns_none():
    """scope={} → no restriction → None."""
    assert build_user_scope({}) is None


def test_build_user_scope_missing_folder_ids_returns_none():
    """scope without folder_ids key → None."""
    assert build_user_scope({"other_key": "value"}) is None


def test_build_user_scope_empty_folder_ids_returns_none():
    """scope={"folder_ids": []} → no restriction → None."""
    assert build_user_scope({"folder_ids": []}) is None


def test_build_user_scope_string_uuids_parsed():
    """scope={"folder_ids": [str(uuid)]} → UserScope with parsed UUIDs."""
    fid = uuid.uuid4()
    result = build_user_scope({"folder_ids": [str(fid)]})
    assert result is not None
    assert isinstance(result, UserScope)
    assert result.folder_ids == [fid]


def test_build_user_scope_multiple_folder_ids():
    """scope with multiple folder_ids → UserScope with all UUIDs."""
    fid1 = uuid.uuid4()
    fid2 = uuid.uuid4()
    result = build_user_scope({"folder_ids": [str(fid1), str(fid2)]})
    assert result is not None
    assert len(result.folder_ids) == 2
    assert fid1 in result.folder_ids
    assert fid2 in result.folder_ids


def test_build_user_scope_uuid_objects_passed_through():
    """scope with UUID objects (not strings) are passed through unchanged."""
    fid = uuid.uuid4()
    result = build_user_scope({"folder_ids": [fid]})
    assert result is not None
    assert result.folder_ids == [fid]


def test_build_user_scope_none_scope_returns_none():
    """scope=None (can happen if column default hasn't fired) → None."""
    assert build_user_scope(None) is None


# ---------------------------------------------------------------------------
# Shared helpers for the wiring integration tests
# ---------------------------------------------------------------------------


def _make_mock_client(call_count_holder, tool_call_chunk, text_chunk):
    """Return a mock httpx.AsyncClient that serves tool-call then text response."""
    from unittest.mock import AsyncMock, MagicMock

    def make_response(lines):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def aiter_lines():
            for line in lines:
                yield line

        mock_resp.aiter_lines = aiter_lines
        mock_resp.aclose = AsyncMock()
        return mock_resp

    def build_response(*args, **kwargs):
        call_count_holder[0] += 1
        if call_count_holder[0] == 1:
            return make_response([f"data: {tool_call_chunk}", "data: [DONE]"])
        else:
            return make_response([f"data: {text_chunk}", "data: [DONE]"])

    mock_client = MagicMock()
    mock_client.build_request = MagicMock(return_value=MagicMock())
    mock_client.send = AsyncMock(side_effect=build_response)
    mock_client.aclose = AsyncMock()
    return mock_client


def _search_tool_call_chunk():
    import json

    return json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "search_documents",
                                    "arguments": '{"query": "test"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )


def _text_chunk(content="Here is the answer."):
    import json

    return json.dumps({"choices": [{"delta": {"content": content}}]})


# ---------------------------------------------------------------------------
# Wiring integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def mock_chat_session_factory(db_session, _engine, monkeypatch):
    """Patch async_session_factory everywhere chat_stream touches it so all DB
    access uses the test engine and its own connections (avoids the 'different
    loop' problem with the global asyncpg pool)."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as session:
            yield session

    # chat.py and topics.py each import async_session_factory at module level;
    # patch both so every DB call inside chat_stream uses the test engine.
    monkeypatch.setattr("harbor_clerk.llm.chat.async_session_factory", _factory)
    monkeypatch.setattr("harbor_clerk.topics.async_session_factory", _factory)


@pytest.mark.asyncio
async def test_chat_stream_passes_user_scope_to_execute_tool(
    db_session,
    admin_user,
    two_folder_corpus,
    mock_chat_session_factory,
):
    """When a conversation has scope.folder_ids, chat_stream passes a matching
    UserScope to execute_tool.

    The LLM is mocked to return one tool call then a text response so the
    tool-calling branch is exercised exactly once.
    """
    import json
    from unittest.mock import patch

    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.conversation import Conversation

    folder_a, _, _, _ = two_folder_corpus

    conv = Conversation(
        user_id=admin_user.user_id,
        title="Scoped chat",
        scope={"folder_ids": [str(folder_a.folder_id)]},
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    captured_scope: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        captured_scope.append(user_scope)
        return json.dumps({"results": [], "total": 0})

    call_count = [0]
    mock_client = _make_mock_client(call_count, _search_tool_call_chunk(), _text_chunk())

    with (
        patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        from harbor_clerk.llm.chat import chat_stream

        async for _ in chat_stream(conv.conversation_id, "test query", admin_user.user_id):
            pass

    assert len(captured_scope) == 1, f"execute_tool called {len(captured_scope)} times, expected 1"
    scope = captured_scope[0]
    assert isinstance(scope, UserScope), f"Expected UserScope, got {type(scope)}"
    assert scope.folder_ids == [folder_a.folder_id], (
        f"Expected folder_ids=[{folder_a.folder_id}], got {scope.folder_ids}"
    )


@pytest.mark.asyncio
async def test_chat_stream_unscoped_conversation_passes_none_scope(
    db_session,
    admin_user,
    mock_chat_session_factory,
):
    """An unscoped conversation → execute_tool receives user_scope=None."""
    import json
    from unittest.mock import patch

    from harbor_clerk.llm import chat as chat_module
    from harbor_clerk.models.conversation import Conversation

    conv = Conversation(
        user_id=admin_user.user_id,
        title="Unscoped chat",
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    captured_scope: list = []

    async def fake_execute_tool(fn_name, fn_args, user_id, *, user_scope=None):
        captured_scope.append(user_scope)
        return json.dumps({"results": [], "total": 0})

    call_count = [0]
    mock_client = _make_mock_client(call_count, _search_tool_call_chunk(), _text_chunk("Answer."))

    with (
        patch.object(chat_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        from harbor_clerk.llm.chat import chat_stream

        async for _ in chat_stream(conv.conversation_id, "hello", admin_user.user_id):
            pass

    assert len(captured_scope) == 1, f"execute_tool called {len(captured_scope)} times, expected 1"
    assert captured_scope[0] is None, f"Expected None scope for unscoped conv, got {captured_scope[0]}"
