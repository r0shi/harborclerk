# Auto-Inject RAG Context in Chat — Design

**Goal:** When a user asks a question in chat, automatically search the knowledge base and inject relevant chunks into the LLM's context before generation starts — giving fast, cited answers without requiring the LLM to explicitly call a search tool.

**Design philosophy:** RAG for the fast path (80%+ of simple Q&A); MCP-style tools remain available for deeper investigation. Transparency at every stage — the user always sees what context the LLM is working with and where it came from.

---

## Architecture

### Backend: Search + Inject in `chat_stream()`

In `llm/chat.py`, after saving the user message and before building the LLM request:

1. Call `hybrid_search(session, user_message, k=rag_auto_k)` (default k=3)
2. Filter results below a score threshold (configurable, default 0.3)
3. If any results pass the threshold, build a context block and prepend to system message
4. Emit a `rag_context` SSE event with structured chunk data for the frontend
5. LLM still has `search_documents` and `read_passages` tools for follow-up investigation

System prompt adjusted: "Relevant context has been provided below. Use it to answer if sufficient; use tools for deeper investigation."

Setting `rag_auto_k = 0` cleanly disables auto-inject (no search, no injection, no SSE event).

### Configuration

Two new settings in `config.py`:
- `rag_auto_k: int = 3` — number of chunks to auto-inject (0 disables)
- `rag_auto_threshold: float = 0.3` — minimum score to include a chunk

### Frontend: RagContextCard

New SSE event type `rag_context` handled in `useChat.ts`. New `RagContextCard` component rendered above assistant response:
- Collapsed: one-line summary ("3 passages from 2 documents")
- Expanded: chunk rows with clickable doc title (→ `/docs/{doc_id}`), page range, tooltip with filename + pages, text preview

Visually lighter than tool call cards to distinguish "auto context" from "LLM-requested tool calls."

### Message Persistence

New nullable `rag_context` JSONB column on `chat_messages` table. Stores the injected chunks array so context cards render correctly when loading conversation history.

### Score Threshold (not stoplist)

No hardcoded stoplist for greetings/filler. The score threshold naturally filters irrelevant queries — "hello" and "thanks" won't match any document chunks above 0.3. Avoids bilingual maintenance burden and edge cases.

---

## Decisions Made

| Decision | Choice | Rationale |
|---|---|---|
| Tool access | Both (auto-inject + tools) | No meaningful tradeoff; tools handle the long tail |
| Relevance filtering | Score threshold only | Stoplist is fragile, bilingual, and redundant with threshold |
| UI display | Subtle collapsible card | Transparency principle; click-through to source documents |
| Chunk count | Configurable (default 3) | Power users can tune for their model's context window |
| Persistence | Dedicated JSONB column | Clean separation from tool_calls semantics |
| Injection point | Backend in chat_stream() | Single code change, ~100ms latency negligible vs generation time |
