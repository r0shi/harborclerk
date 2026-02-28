# Auto-Inject RAG Context in Chat — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically search the knowledge base when the user sends a chat message, inject relevant chunks into the LLM's context, and show the user exactly what context was provided with click-through links to source documents.

**Architecture:** Before each LLM call in `chat_stream()`, run `hybrid_search()` on the user message. Filter results by score threshold. If any pass, prepend a context block to the system message and emit a `rag_context` SSE event. The frontend renders this as a subtle, collapsible `RagContextCard` above the assistant's response. A new `rag_context` JSONB column on `chat_messages` persists the injected context for conversation history.

**Tech Stack:** Python/FastAPI (backend), React/TypeScript/Tailwind (frontend), PostgreSQL (migration), SQLAlchemy 2.0 async

---

## Task 1: Migration — Add `rag_context` column to `chat_messages`

**Files:**
- Create: `alembic/versions/0013_add_rag_context_column.py`

**Step 1: Create migration file**

```python
"""Add rag_context JSONB column to chat_messages."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("rag_context", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "rag_context")
```

**Step 2: Run migration to verify**

```bash
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka_test" alembic upgrade head
```

Expected: Migration applies without error.

**Step 3: Commit**

```bash
git add alembic/versions/0013_add_rag_context_column.py
git commit -m "migration: add rag_context JSONB column to chat_messages"
```

---

## Task 2: Model + Config updates

**Files:**
- Modify: `src/harbor_clerk/models/chat_message.py` (add `rag_context` field)
- Modify: `src/harbor_clerk/config.py` (add RAG settings)

**Step 1: Add `rag_context` field to ChatMessage model**

In `src/harbor_clerk/models/chat_message.py`, add after `tool_call_id`:

```python
rag_context: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
```

This matches the existing `tool_calls` pattern exactly.

**Step 2: Add RAG settings to config.py**

In `src/harbor_clerk/config.py`, add after the `# MCP search defaults` block:

```python
# RAG auto-inject in chat
rag_auto_k: int = Field(default=3)
rag_auto_threshold: float = Field(default=0.3)
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/models/chat_message.py src/harbor_clerk/config.py
git commit -m "feat: add rag_context model field and RAG config settings"
```

---

## Task 3: Backend — Auto-inject RAG context in `chat_stream()`

**Files:**
- Modify: `src/harbor_clerk/llm/chat.py`

This is the core change. In `chat_stream()`, after loading history and before the tool-calling loop, we:
1. Run `hybrid_search()` with the user message
2. Filter results below `rag_auto_threshold`
3. If results pass, build a context block and adjust the system message
4. Emit a `rag_context` SSE event
5. Store the context on the assistant's ChatMessage

**Step 1: Add import for hybrid_search**

At the top of `chat.py`, add:

```python
from harbor_clerk.search import hybrid_search
```

**Step 2: Add RAG injection logic**

Replace the block that builds the `messages` list (starting at `messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]` through to `messages.append(entry)`) with the following expanded version:

```python
        # --- RAG auto-inject ---
        rag_chunks: list[dict] | None = None
        system_content = SYSTEM_PROMPT

        if settings.rag_auto_k > 0:
            try:
                search_result = await hybrid_search(
                    session, user_message, k=settings.rag_auto_k,
                )
                good_hits = [
                    h for h in search_result.hits
                    if h.score >= settings.rag_auto_threshold
                ]
                if good_hits:
                    rag_chunks = [
                        {
                            "chunk_id": h.chunk_id,
                            "doc_id": h.doc_id,
                            "doc_title": h.doc_title or "Untitled",
                            "page_start": h.page_start,
                            "page_end": h.page_end,
                            "score": round(h.score, 3),
                            "text": h.chunk_text[:200],
                        }
                        for h in good_hits
                    ]

                    # Build context block for LLM
                    context_lines = ["\n\n[Relevant context from knowledge base]\n"]
                    for h in good_hits:
                        pages = (
                            f"page {h.page_start}"
                            if h.page_start == h.page_end
                            else f"pages {h.page_start}-{h.page_end}"
                        )
                        context_lines.append(
                            f'Document: "{h.doc_title or "Untitled"}" ({pages})\n'
                            f"{h.chunk_text}\n"
                        )
                    context_lines.append(
                        "[End of context — use search_documents for additional investigation if needed]"
                    )
                    system_content = SYSTEM_PROMPT_WITH_CONTEXT + "\n".join(context_lines)
            except Exception:
                logger.debug("RAG auto-inject search failed, continuing without context", exc_info=True)

        # Build messages for the LLM
        messages: list[dict] = [{"role": "system", "content": system_content}]
        for msg in history_rows[-MAX_HISTORY_MESSAGES:]:
            entry: dict = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
                entry.pop("content", None)
                if not msg.content:
                    entry["content"] = ""
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)

        # Emit RAG context event if we injected chunks
        if rag_chunks:
            yield f"data: {json.dumps({'type': 'rag_context', 'chunks': rag_chunks})}\n\n"
```

**Step 3: Add the alternative system prompt constant**

After the existing `SYSTEM_PROMPT`, add:

```python
SYSTEM_PROMPT_WITH_CONTEXT = (
    "You are Harbor Clerk, a document assistant for a local knowledge base. "
    "Relevant context from the knowledge base has been provided below. "
    "Use it to answer the user's question if sufficient. "
    "If you need more information, use the search_documents tool for additional investigation. "
    "Always cite your sources with document titles and page numbers. "
    "If you cannot find relevant information, say so honestly. "
    "Respond in the same language as the user's question. Be concise and factual."
)
```

**Step 4: Store rag_context on the assistant message**

In the block where the assistant message is saved (after `if assistant_content:`), modify to include `rag_context`:

```python
        if assistant_content:
            assistant_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                tokens_used=total_tokens or None,
                rag_context=rag_chunks,
            )
            session.add(assistant_msg)
```

**Step 5: Verify existing tests still pass**

```bash
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka_test" uv run pytest tests/ -v -x
```

Expected: All existing tests pass (the RAG injection is a no-op when no embedder is available in tests).

**Step 6: Commit**

```bash
git add src/harbor_clerk/llm/chat.py
git commit -m "feat: auto-inject RAG context in chat_stream"
```

---

## Task 4: Backend tests for RAG injection

**Files:**
- Create: `tests/test_chat_rag.py`

These tests verify the RAG injection logic in isolation by mocking `hybrid_search`.

**Step 1: Write tests**

```python
"""Tests for RAG auto-inject in chat."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.models import User
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.enums import UserRole
from harbor_clerk.auth import create_access_token, hash_password
from tests.conftest import auth_header


@pytest.fixture
async def chat_user(db_session) -> User:
    user = User(
        email="chatuser@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
def chat_token(chat_user: User) -> str:
    return create_access_token(chat_user.user_id, chat_user.role.value)


@pytest.fixture
async def conversation(db_session, chat_user) -> Conversation:
    conv = Conversation(user_id=chat_user.user_id, title="Test conversation")
    db_session.add(conv)
    await db_session.flush()
    return conv


def _make_search_result(hits):
    """Build a SearchResult with given hits."""
    from harbor_clerk.search import SearchResult, SearchHit
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


async def test_rag_context_emitted_when_hits_above_threshold(
    client, chat_user, chat_token, conversation, db_session,
):
    """When search returns high-scoring hits, a rag_context SSE event is emitted."""
    mock_result = _make_search_result([
        {"score": 0.8, "title": "Policy Manual", "text": "Section 4 covers compliance."},
        {"score": 0.6, "title": "Report Q3", "text": "Revenue increased by 12%."},
    ])

    with patch("harbor_clerk.llm.chat.hybrid_search", new_callable=AsyncMock, return_value=mock_result):
        with patch("harbor_clerk.llm.chat.httpx.AsyncClient") as mock_client_cls:
            # Mock the LLM to return a simple text response (no tool calls)
            mock_response = AsyncMock()
            mock_response.raise_for_status = lambda: None
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            # Simulate SSE lines from LLM
            async def fake_lines():
                yield 'data: {"choices":[{"delta":{"content":"Based on the context"}}]}'
                yield "data: [DONE]"

            mock_response.aiter_lines = fake_lines
            mock_stream_ctx = AsyncMock()
            mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_http = AsyncMock()
            mock_http.stream.return_value = mock_stream_ctx
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_http

            resp = await client.post(
                f"/api/chat/conversations/{conversation.conversation_id}/messages",
                json={"content": "What does the policy say about compliance?"},
                headers=auth_header(chat_token),
            )
            assert resp.status_code == 200

            # Parse SSE events
            events = []
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass

            # Should have rag_context event
            rag_events = [e for e in events if e.get("type") == "rag_context"]
            assert len(rag_events) == 1
            chunks = rag_events[0]["chunks"]
            assert len(chunks) == 2
            assert chunks[0]["doc_title"] == "Policy Manual"
            assert chunks[0]["score"] >= 0.3


async def test_rag_context_skipped_when_scores_below_threshold(
    client, chat_user, chat_token, conversation, db_session,
):
    """When all search results score below threshold, no rag_context event is emitted."""
    mock_result = _make_search_result([
        {"score": 0.1, "title": "Irrelevant Doc"},
    ])

    with patch("harbor_clerk.llm.chat.hybrid_search", new_callable=AsyncMock, return_value=mock_result):
        with patch("harbor_clerk.llm.chat.httpx.AsyncClient") as mock_client_cls:
            mock_response = AsyncMock()
            mock_response.raise_for_status = lambda: None
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            async def fake_lines():
                yield 'data: {"choices":[{"delta":{"content":"Hello!"}}]}'
                yield "data: [DONE]"

            mock_response.aiter_lines = fake_lines
            mock_stream_ctx = AsyncMock()
            mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_http = AsyncMock()
            mock_http.stream.return_value = mock_stream_ctx
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_http

            resp = await client.post(
                f"/api/chat/conversations/{conversation.conversation_id}/messages",
                json={"content": "hello there"},
                headers=auth_header(chat_token),
            )
            assert resp.status_code == 200

            events = []
            for line in resp.text.split("\n"):
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass

            rag_events = [e for e in events if e.get("type") == "rag_context"]
            assert len(rag_events) == 0


async def test_rag_disabled_when_k_is_zero(
    client, chat_user, chat_token, conversation, db_session,
):
    """When rag_auto_k=0, no search is performed."""
    with patch("harbor_clerk.llm.chat.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.rag_auto_k = 0
        settings.rag_auto_threshold = 0.3
        settings.llama_server_url = "http://localhost:8102"

        with patch("harbor_clerk.llm.chat.hybrid_search", new_callable=AsyncMock) as mock_search:
            with patch("harbor_clerk.llm.chat.httpx.AsyncClient") as mock_client_cls:
                mock_response = AsyncMock()
                mock_response.raise_for_status = lambda: None
                mock_response.__aenter__ = AsyncMock(return_value=mock_response)
                mock_response.__aexit__ = AsyncMock(return_value=False)

                async def fake_lines():
                    yield 'data: {"choices":[{"delta":{"content":"Hi!"}}]}'
                    yield "data: [DONE]"

                mock_response.aiter_lines = fake_lines
                mock_stream_ctx = AsyncMock()
                mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
                mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_http = AsyncMock()
                mock_http.stream.return_value = mock_stream_ctx
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_http

                resp = await client.post(
                    f"/api/chat/conversations/{conversation.conversation_id}/messages",
                    json={"content": "What is this about?"},
                    headers=auth_header(chat_token),
                )
                assert resp.status_code == 200

                # hybrid_search should NOT have been called
                mock_search.assert_not_called()
```

**Step 2: Run tests to verify they pass**

```bash
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka_test" uv run pytest tests/test_chat_rag.py -v
```

Expected: All 3 tests pass.

**Step 3: Commit**

```bash
git add tests/test_chat_rag.py
git commit -m "test: add RAG auto-inject backend tests"
```

---

## Task 5: Frontend — Handle `rag_context` SSE event in `useChat.ts`

**Files:**
- Modify: `frontend/src/hooks/useChat.ts`

**Step 1: Add `rag_context` to the ChatMessage type**

Update the `ChatMessage` interface:

```typescript
export interface ChatMessage {
  message_id?: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCallInfo[]
  rag_context?: RagContextChunk[]
  isStreaming?: boolean
}
```

Add the `RagContextChunk` type (re-export from the component):

```typescript
export interface RagContextChunk {
  chunk_id: string
  doc_id: string
  doc_title: string
  page_start: number
  page_end: number
  score: number
  text: string
}
```

**Step 2: Handle the `rag_context` SSE event**

In the `switch (event.type)` block inside `sendMessage`, add a new case before the `token` case:

```typescript
case 'rag_context':
  setMessages((prev) => {
    const updated = [...prev]
    const last = updated[updated.length - 1]
    if (last && last.role === 'assistant') {
      updated[updated.length - 1] = {
        ...last,
        rag_context: event.chunks,
      }
    }
    return updated
  })
  break
```

**Step 3: Commit**

```bash
git add frontend/src/hooks/useChat.ts
git commit -m "feat: handle rag_context SSE event in useChat hook"
```

---

## Task 6: Frontend — Render `RagContextCard` in `ChatPage.tsx`

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx`
- Already created: `frontend/src/components/RagContextCard.tsx`
- Already created: CSS animations in `frontend/src/index.css`

The `RagContextCard` component and its CSS animation were already created during the frontend design pass.

**Step 1: Import the component in ChatPage.tsx**

Add at the top of `ChatPage.tsx`:

```typescript
import RagContextCard from '../components/RagContextCard'
```

**Step 2: Render RagContextCard inside MessageBubble**

In the `MessageBubble` component, add the `RagContextCard` rendering inside the message content `div`, just before the tool_calls block. The relevant section to modify is in the `<div className={`rounded-xl px-4 py-2.5 ...`}>` block:

```tsx
{/* RAG context card — shown above tool calls and text */}
{!isUser && message.rag_context && message.rag_context.length > 0 && (
  <div className="mb-2.5">
    <RagContextCard chunks={message.rag_context} />
  </div>
)}

{/* Tool calls shown as inline cards */}
{message.tool_calls && message.tool_calls.length > 0 && (
  ...existing code...
)}
```

**Step 3: Also handle rag_context when loading conversation history**

In the `useEffect` that loads conversation history (the `get<ConversationDetail>` call), ensure `rag_context` is passed through. Update the message mapping:

```typescript
.map((m) => ({
  message_id: m.message_id,
  role: m.role as ChatMessage['role'],
  content: m.content,
  rag_context: m.rag_context,
})),
```

This requires adding `rag_context` to the `ConversationDetail.messages` type as well:

```typescript
interface ConversationDetail extends ConversationSummary {
  messages: {
    message_id: string
    role: string
    content: string
    tool_calls?: unknown[]
    tool_call_id?: string
    tokens_used?: number
    rag_context?: RagContextChunk[]
    created_at: string
  }[]
}
```

Import `RagContextChunk` from the hook:

```typescript
import { useChat, type ChatMessage, type ToolCallInfo, type RagContextChunk } from '../hooks/useChat'
```

(Remove `RagContextChunk` from the component file export since it's now in the hook.)

**Step 4: Build and verify**

```bash
cd frontend && npm run build
```

Expected: Build succeeds with no type errors.

**Step 5: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx frontend/src/components/RagContextCard.tsx frontend/src/hooks/useChat.ts frontend/src/index.css
git commit -m "feat: render RagContextCard in chat UI with click-through to documents"
```

---

## Task 7: Backend — Include `rag_context` in conversation history API

**Files:**
- Modify: `src/harbor_clerk/api/schemas/chat.py`

The `ChatMessageOut` schema needs to include `rag_context` so it's returned when loading conversation history.

**Step 1: Add `rag_context` to ChatMessageOut**

In `src/harbor_clerk/api/schemas/chat.py`, update the `ChatMessageOut` class:

```python
class ChatMessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    tool_calls: Any | None = None
    tool_call_id: str | None = None
    tokens_used: int | None = None
    rag_context: Any | None = None
    created_at: datetime
```

**Step 2: Verify the full test suite passes**

```bash
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka_test" uv run pytest tests/ -v
```

Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/harbor_clerk/api/schemas/chat.py
git commit -m "feat: include rag_context in chat message API response"
```

---

## Task 8: Update roadmap and memory

**Files:**
- Modify: `docs/plans/2026-02-26-mcp-enhancements-roadmap.md`

**Step 1: Mark Feature 9 as Done**

Update the status table row for Feature 9 from `Not started` to `**Done**`.

**Step 2: Commit**

```bash
git add docs/plans/2026-02-26-mcp-enhancements-roadmap.md
git commit -m "docs: mark Feature 9 (Auto-Inject RAG) as done"
```

---

## Verification Checklist

```bash
# Run all backend tests
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka_test" uv run pytest tests/ -v

# Build frontend
cd frontend && npm run build

# Manual verification:
# 1. Start the app, open chat
# 2. Ask a question about an ingested document
# 3. Verify: RagContextCard appears above assistant response (collapsed)
# 4. Expand the card — verify document titles are clickable links to /docs/{doc_id}
# 5. Verify tooltip on hover shows filename + pages
# 6. Ask "hello" — verify no RagContextCard appears (score threshold filters it)
# 7. Navigate away and back to conversation — verify RagContextCard renders from history
```
