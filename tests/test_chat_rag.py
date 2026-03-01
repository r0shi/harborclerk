"""Tests for RAG auto-inject in chat."""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from harbor_clerk.config import get_settings
from harbor_clerk.search import SearchHit, SearchResult


def _make_search_result(hits):
    """Build a SearchResult with given hits."""
    return SearchResult(
        hits=[
            SearchHit(
                chunk_id=str(uuid.uuid4()),
                doc_id=str(uuid.uuid4()),
                version_id=str(uuid.uuid4()),
                chunk_num=0,
                chunk_text=h.get("text", "Some relevant text from the document."),
                page_start=h.get("page_start", 1),
                page_end=h.get("page_end", 1),
                language="english",
                ocr_used=False,
                ocr_confidence=None,
                score=h["score"],
                doc_title=h.get("title", "Test Document"),
            )
            for h in hits
        ],
        total_candidates=len(hits),
    )


def _mock_llm_streaming(response_text="Hello!"):
    """Create mock httpx client that streams a simple LLM response.

    httpx.AsyncClient is used as:
        async with httpx.AsyncClient(...) as client:      # outer async CM
            async with client.stream("POST", ...) as r:   # inner async CM
                async for line in r.aiter_lines(): ...

    client.stream() is a *sync* call returning an async CM, so mock_http
    must be a MagicMock (not AsyncMock) for .stream().
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    async def fake_lines():
        yield f'data: {{"choices":[{{"delta":{{"content":"{response_text}"}}}}]}}'
        yield "data: [DONE]"

    mock_response.aiter_lines = fake_lines

    # Inner context manager: async with client.stream(...) as response
    mock_stream = AsyncMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=False)

    # client object returned by outer __aenter__
    mock_http = MagicMock()  # sync — stream() is not async
    mock_http.stream.return_value = mock_stream

    # Outer context manager: async with httpx.AsyncClient(...) as client
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_http)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return mock_client


async def _collect_events(gen):
    """Collect SSE events from a chat_stream generator."""
    events = []
    async for line in gen:
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:].strip()))
            except json.JSONDecodeError:
                pass
    return events


async def test_rag_context_emitted_when_hits_above_threshold(db_session):
    """When search returns high-scoring hits, a rag_context SSE event is emitted."""
    from harbor_clerk.auth import hash_password
    from harbor_clerk.models import User
    from harbor_clerk.models.conversation import Conversation
    from harbor_clerk.models.enums import UserRole

    # Create user and conversation (committed so chat_stream's session sees them)
    user = User(
        email="rag_test@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    conv = Conversation(user_id=user.user_id, title="RAG test")
    db_session.add(conv)
    await db_session.flush()

    mock_result = _make_search_result(
        [
            {
                "score": 0.8,
                "title": "Policy Manual",
                "text": "Section 4 covers compliance.",
            },
            {"score": 0.6, "title": "Report Q3", "text": "Revenue increased by 12%."},
        ]
    )

    settings = get_settings()
    original_model = settings.llm_model_id
    settings.llm_model_id = "test-model"

    @asynccontextmanager
    async def fake_session_factory():
        yield db_session

    try:
        from harbor_clerk.llm.chat import chat_stream

        with (
            patch(
                "harbor_clerk.llm.chat.async_session_factory",
                fake_session_factory,
            ),
            patch(
                "harbor_clerk.llm.chat.hybrid_search",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "harbor_clerk.llm.chat.httpx.AsyncClient",
                return_value=_mock_llm_streaming("Based on the context"),
            ),
        ):
            events = await _collect_events(chat_stream(conv.conversation_id, "What does the policy say?"))
    finally:
        settings.llm_model_id = original_model

    rag_events = [e for e in events if e.get("type") == "rag_context"]
    assert len(rag_events) == 1
    chunks = rag_events[0]["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["doc_title"] == "Policy Manual"
    assert chunks[0]["score"] >= 0.3

    # Should also have token and done events
    token_events = [e for e in events if e.get("type") == "token"]
    assert len(token_events) > 0
    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1


async def test_rag_context_skipped_when_scores_below_threshold(db_session):
    """When all search results score below threshold, no rag_context event."""
    from harbor_clerk.auth import hash_password
    from harbor_clerk.models import User
    from harbor_clerk.models.conversation import Conversation
    from harbor_clerk.models.enums import UserRole

    user = User(
        email="rag_test2@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    conv = Conversation(user_id=user.user_id, title="RAG test 2")
    db_session.add(conv)
    await db_session.flush()

    mock_result = _make_search_result([{"score": 0.1, "title": "Irrelevant Doc"}])

    settings = get_settings()
    original_model = settings.llm_model_id
    settings.llm_model_id = "test-model"

    @asynccontextmanager
    async def fake_session_factory():
        yield db_session

    try:
        from harbor_clerk.llm.chat import chat_stream

        with (
            patch(
                "harbor_clerk.llm.chat.async_session_factory",
                fake_session_factory,
            ),
            patch(
                "harbor_clerk.llm.chat.hybrid_search",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "harbor_clerk.llm.chat.httpx.AsyncClient",
                return_value=_mock_llm_streaming("Hello!"),
            ),
        ):
            events = await _collect_events(chat_stream(conv.conversation_id, "hello there"))
    finally:
        settings.llm_model_id = original_model

    rag_events = [e for e in events if e.get("type") == "rag_context"]
    assert len(rag_events) == 0


async def test_rag_context_skipped_when_search_fails(db_session):
    """When hybrid_search raises, chat continues without RAG context."""
    from harbor_clerk.auth import hash_password
    from harbor_clerk.models import User
    from harbor_clerk.models.conversation import Conversation
    from harbor_clerk.models.enums import UserRole

    user = User(
        email="rag_test3@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    conv = Conversation(user_id=user.user_id, title="RAG test 3")
    db_session.add(conv)
    await db_session.flush()

    settings = get_settings()
    original_model = settings.llm_model_id
    settings.llm_model_id = "test-model"

    @asynccontextmanager
    async def fake_session_factory():
        yield db_session

    try:
        from harbor_clerk.llm.chat import chat_stream

        with (
            patch(
                "harbor_clerk.llm.chat.async_session_factory",
                fake_session_factory,
            ),
            patch(
                "harbor_clerk.llm.chat.hybrid_search",
                new_callable=AsyncMock,
                side_effect=RuntimeError("embedder down"),
            ),
            patch(
                "harbor_clerk.llm.chat.httpx.AsyncClient",
                return_value=_mock_llm_streaming("I can still respond"),
            ),
        ):
            events = await _collect_events(chat_stream(conv.conversation_id, "What is this about?"))
    finally:
        settings.llm_model_id = original_model

    rag_events = [e for e in events if e.get("type") == "rag_context"]
    assert len(rag_events) == 0
    token_events = [e for e in events if e.get("type") == "token"]
    assert len(token_events) > 0
