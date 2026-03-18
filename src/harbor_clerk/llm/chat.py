"""Chat orchestration — streaming tool-calling loop against llama-server."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.models import get_model
from harbor_clerk.llm.tools import CHAT_TOOLS, execute_tool
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

logger = logging.getLogger(__name__)

# SSE keepalive interval — prevents idle connection drops during LLM thinking.
_KEEPALIVE_INTERVAL = 30.0

# Rough chars-per-token estimate for budget calculations.
# Conservative (undercounts tokens → keeps us safely under the limit).
_CHARS_PER_TOKEN = 3.5

# Reserve this fraction of context window for the model's response.
_RESPONSE_RESERVE = 0.20

# Stop the tool loop when context usage exceeds this fraction, leaving room
# for the model to generate a text response.
_TOOL_LOOP_BUDGET = 0.75

# Hard safety cap on tool rounds (prevents infinite loops even if budget check fails).
_MAX_TOOL_ROUNDS = 25

# Approximate token overhead for the tool schema (CHAT_TOOLS JSON).
# Computed once at module load to avoid re-serializing every call.
_TOOL_SCHEMA_TOKENS: int | None = None


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return int(len(text) / _CHARS_PER_TOKEN)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a message list."""
    total = 0
    for msg in messages:
        total += _estimate_tokens(msg.get("content", ""))
        if msg.get("tool_calls"):
            total += _estimate_tokens(json.dumps(msg["tool_calls"]))
    # Per-message overhead (~4 tokens each for role, separators)
    total += len(messages) * 4
    return total


def _get_tool_schema_tokens() -> int:
    """Lazily compute and cache the tool schema token estimate."""
    global _TOOL_SCHEMA_TOKENS
    if _TOOL_SCHEMA_TOKENS is None:
        _TOOL_SCHEMA_TOKENS = _estimate_tokens(json.dumps(CHAT_TOOLS))
    return _TOOL_SCHEMA_TOKENS


def _context_usage(messages: list[dict], context_window: int) -> float:
    """Return fraction of context window used by current messages + tool schema."""
    if context_window <= 0:
        return 0.0
    tokens = _estimate_messages_tokens(messages) + _get_tool_schema_tokens()
    return tokens / context_window


def _trim_to_budget(messages: list[dict], context_window: int) -> list[dict]:
    """Trim oldest history messages to fit within context budget.

    Keeps the system prompt (index 0) and the most recent user message
    (last entry). Removes messages from the front of history, keeping
    tool call/result pairs together.

    Returns a new list (does not mutate the input).
    """
    budget = int(context_window * (1 - _RESPONSE_RESERVE))
    budget -= _get_tool_schema_tokens()

    if _estimate_messages_tokens(messages) <= budget:
        return messages

    # System prompt is always kept (index 0). Trim from index 1 onward.
    system = messages[:1]
    history = messages[1:]

    # Remove from the front of history until we fit
    while history and _estimate_messages_tokens(system + history) > budget:
        removed = history.pop(0)
        # If we removed an assistant message with tool_calls, also remove
        # the following tool result messages (they reference it).
        while history and history[0].get("role") == "tool":
            history.pop(0)
        # If we removed a tool result, check if the preceding assistant
        # message's tool calls are now orphaned (skip — rare edge case).
        if removed.get("role") == "tool":
            continue

    if not history:
        # Extreme case: even system + user message exceeds budget.
        # Keep them anyway — llama-server will handle the overflow.
        return messages

    trimmed = system + history
    if len(trimmed) < len(messages):
        logger.info(
            "Trimmed %d messages from history to fit context budget (%d tokens)",
            len(messages) - len(trimmed),
            budget,
        )
    return trimmed


_CORE_INSTRUCTIONS = (
    "You are Harbor Clerk, a document assistant for a local knowledge base.\n\n"
    "## How to answer questions\n\n"
    "Ground answers in the corpus whenever possible. When citing documents, "
    "always use this format: [Document Title, page X].\n\n"
    "Never fabricate document content or citations. If you search and find "
    "insufficient evidence, say so clearly rather than guessing.\n\n"
    "You may supplement with general knowledge for context or explanation, "
    "but always make corpus-sourced claims identifiable by their citations. "
    "The absence of a citation signals general knowledge — never cite a "
    "document you did not retrieve.\n\n"
    "## Choosing the right tool\n\n"
    "**Always search first.** For any question about document content — "
    "whether specific or broad — start with search_documents. "
    "It finds relevant passages across the entire corpus, even with hundreds of documents.\n\n"
    "For factual questions:\n"
    "  search_documents → read_passages → expand_context if needed\n\n"
    'For broad or comparative questions ("compare all X", "what does the corpus say about Y"):\n'
    "  search_documents with several different queries to cover the topic thoroughly\n\n"
    'For structural questions ("how many documents?", "what file types?"):\n'
    "  corpus_overview\n\n"
    "For browsing recent changes:\n"
    "  list_documents (shows a paginated subset, not the full corpus)\n\n"
    'For entity questions ("who is mentioned?", "what organizations?"):\n'
    "  entity_search, entity_overview, or entity_cooccurrence\n\n"
    "For document comparison or discovery:\n"
    "  find_related\n\n"
    "For processing status:\n"
    "  ingest_status\n\n"
    "## Context management\n\n"
    "Strongly prefer searching for specific passages over reading entire "
    "documents. The workflow search_documents → read_passages → expand_context "
    "uses far less context than read_document. Only use read_document for "
    "specific page ranges after checking document_outline — never to read "
    "a full document end-to-end.\n\n"
    "## Language\n\n"
    "Respond in the same language as the user's question."
)

SYSTEM_PROMPT = _CORE_INSTRUCTIONS

# Sentinel yielded by _iter_with_keepalive when the LLM is idle.
_KEEPALIVE_SENTINEL = object()


async def _iter_with_keepalive(aiter):
    """Wrap an async iterator, yielding ``_KEEPALIVE_SENTINEL`` every
    ``_KEEPALIVE_INTERVAL`` seconds of inactivity.

    This lets the outer generator yield SSE keepalive comments so
    Starlette can detect client disconnects during long LLM thinks.
    """
    ait = aiter.__aiter__()
    while True:
        try:
            nxt = asyncio.ensure_future(ait.__anext__())
            done, _ = await asyncio.wait({nxt}, timeout=_KEEPALIVE_INTERVAL)
            if done:
                yield nxt.result()
            else:
                yield _KEEPALIVE_SENTINEL
        except StopAsyncIteration:
            return


async def chat_stream(
    conversation_id: uuid.UUID,
    user_message: str,
    user_id: uuid.UUID | None = None,
) -> AsyncGenerator[str, None]:
    """Stream chat response as SSE events. Handles tool-calling loop internally.

    Creates its own DB session so it is not tied to the FastAPI DI lifecycle
    (the DI session closes when the endpoint returns, before the SSE generator
    has finished streaming).
    """
    settings = get_settings()
    active_model_id = settings.llm_model_id or None

    # Compute per-tool-result truncation limit from model context window.
    # Reserve ~25% of context for tool results (rest: system prompt, tools, history, response).
    # ~3.5 chars per token as a conservative estimate.
    model = get_model(settings.llm_model_id) if settings.llm_model_id else None
    if model and settings.llm_yarn_enabled and model.yarn:
        context_tokens = model.yarn.extended_context
    else:
        context_tokens = model.context_window if model else 32768
    tool_result_max_chars = min(int(context_tokens * 0.25 * 3.5), 80_000)

    async with async_session_factory() as session:
        # Save user message
        user_msg = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            content=user_message,
        )
        session.add(user_msg)
        await session.flush()

        # Auto-title immediately on first message so the sidebar updates before LLM responds
        conv = await session.get(Conversation, conversation_id)
        if conv and conv.title == "New conversation":
            conv.title = _generate_title(user_message)
            await session.commit()
            # Emit title event immediately so the frontend can update the sidebar
            yield f"data: {json.dumps({'type': 'title', 'title': conv.title})}\n\n"

        # Load conversation history
        history_result = await session.execute(
            select(ChatMessage).where(ChatMessage.conversation_id == conversation_id).order_by(ChatMessage.created_at)
        )
        history_rows = history_result.scalars().all()

        # Build messages for the LLM
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in history_rows[-settings.max_history_messages :]:
            content = _truncate_for_llm(msg.content, tool_result_max_chars) if msg.role == "tool" else msg.content
            entry: dict = {"role": msg.role, "content": content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
                entry.pop("content", None)
                if not msg.content:
                    entry["content"] = ""
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)

        # Trim history to fit within context budget
        messages = _trim_to_budget(messages, context_tokens)

        # Tool-calling loop — runs until the model produces a text response,
        # context budget is exhausted, or we hit the hard safety cap.
        assistant_content = ""
        total_tokens = 0
        budget_exhausted = False

        for _round in range(_MAX_TOOL_ROUNDS):
            # Check context budget before each LLM call
            usage_frac = _context_usage(messages, context_tokens)
            if usage_frac >= _TOOL_LOOP_BUDGET and _round > 0:
                budget_exhausted = True
                logger.info(
                    "Context budget %.0f%% >= %.0f%% after %d tool rounds, forcing text response (conversation=%s)",
                    usage_frac * 100,
                    _TOOL_LOOP_BUDGET * 100,
                    _round,
                    conversation_id,
                )
                break

            tool_calls_accumulated: list[dict] = []
            text_buffer = ""

            # If budget is getting tight, omit tool definitions so the model
            # generates a text response instead of requesting more tool calls.
            send_tools = CHAT_TOOLS if usage_frac < _TOOL_LOOP_BUDGET else None

            try:
                payload: dict = {
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.3,
                }
                if send_tools:
                    payload["tools"] = send_tools

                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client,
                    client.stream(
                        "POST",
                        f"{settings.llama_server_url}/v1/chat/completions",
                        json=payload,
                    ) as response,
                ):
                    if response.status_code >= 400:
                        await response.aread()
                        detail = response.text[:2000]

                        # Detect context overflow from llama-server
                        is_context_overflow = response.status_code == 400 and any(
                            hint in detail.lower()
                            for hint in ("context length", "too long", "context window", "max_tokens", "n_ctx")
                        )
                        if is_context_overflow:
                            error_summary = "Context window full — please start a new conversation"
                        else:
                            error_summary = f"LLM error ({response.status_code})"
                        error_content = f"Error: {error_summary}"
                        if detail:
                            error_content += f"\n\n{detail}"
                        session.add(
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=error_content,
                                model_id=active_model_id,
                                context_pct=100 if is_context_overflow else None,
                            )
                        )
                        conv = await session.get(Conversation, conversation_id)
                        if conv and conv.title == "New conversation":
                            conv.title = _generate_title(user_message)
                        await session.commit()
                        error_event: dict = {"type": "error", "message": error_summary}
                        if detail:
                            error_event["detail"] = detail
                        yield f"data: {json.dumps(error_event)}\n\n"
                        done_payload: dict = {
                            "type": "done",
                            "context_pct": 100 if is_context_overflow else None,
                        }
                        if conv and conv.title != "New conversation":
                            done_payload["title"] = conv.title
                        if active_model_id:
                            done_payload["model_id"] = active_model_id
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return

                    async for line in _iter_with_keepalive(response.aiter_lines()):
                        if line is _KEEPALIVE_SENTINEL:
                            yield ": keepalive\n\n"
                            continue
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        usage = chunk.get("usage")
                        if usage:
                            total_tokens += usage.get("total_tokens", 0)

                        # Accumulate tool calls from deltas
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                while len(tool_calls_accumulated) <= idx:
                                    tool_calls_accumulated.append(
                                        {
                                            "id": "",
                                            "type": "function",
                                            "function": {
                                                "name": "",
                                                "arguments": "",
                                            },
                                        }
                                    )
                                if "id" in tc and tc["id"]:
                                    tool_calls_accumulated[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if "name" in fn and fn["name"]:
                                    tool_calls_accumulated[idx]["function"]["name"] = fn["name"]
                                if "arguments" in fn:
                                    tool_calls_accumulated[idx]["function"]["arguments"] += fn["arguments"]

                        # Stream text tokens
                        if "content" in delta and delta["content"]:
                            token_text = delta["content"]
                            text_buffer += token_text
                            yield f"data: {json.dumps({'type': 'token', 'content': token_text})}\n\n"

            except (httpx.ConnectError, httpx.TimeoutException):
                error_summary = "LLM server is not running. Select and activate a model in Settings."
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"Error: {error_summary}",
                        model_id=active_model_id,
                    )
                )
                conv = await session.get(Conversation, conversation_id)
                if conv and conv.title == "New conversation":
                    conv.title = _generate_title(user_message)
                await session.commit()
                yield f"data: {json.dumps({'type': 'error', 'message': error_summary})}\n\n"
                done_payload: dict = {"type": "done"}
                if conv and conv.title != "New conversation":
                    done_payload["title"] = conv.title
                if active_model_id:
                    done_payload["model_id"] = active_model_id
                yield f"data: {json.dumps(done_payload)}\n\n"
                return

            # If we got tool calls, execute them and loop
            if tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]:
                # Save assistant tool-call message
                tc_msg = ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content="",
                    tool_calls=tool_calls_accumulated,
                )
                session.add(tc_msg)
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls_accumulated,
                    }
                )

                for tc in tool_calls_accumulated:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'arguments': fn_args})}\n\n"

                    result_str = await execute_tool(fn_name, fn_args, user_id)

                    yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': _summarize_result(result_str)})}\n\n"

                    # Save full result to DB, but truncate for LLM context
                    tool_msg = ChatMessage(
                        conversation_id=conversation_id,
                        role="tool",
                        content=result_str,
                        tool_call_id=tc.get("id", f"call_{fn_name}"),
                    )
                    session.add(tool_msg)
                    truncated_result = _truncate_for_llm(result_str, tool_result_max_chars)
                    messages.append(
                        {
                            "role": "tool",
                            "content": truncated_result,
                            "tool_call_id": tc.get("id", f"call_{fn_name}"),
                        }
                    )

                await session.flush()
                continue  # Next round — let LLM see tool results

            # No tool calls — we have the final text response
            assistant_content = text_buffer
            break

        # If the loop ended without a text response (budget exhausted or
        # hard cap hit), make one final LLM call without tools to force
        # a text response from whatever context we have.
        if not assistant_content and (budget_exhausted or _round == _MAX_TOOL_ROUNDS - 1):
            reason = "context budget" if budget_exhausted else f"{_MAX_TOOL_ROUNDS} tool rounds"
            logger.info("Forcing final text response after %s (conversation=%s)", reason, conversation_id)
            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client,
                    client.stream(
                        "POST",
                        f"{settings.llama_server_url}/v1/chat/completions",
                        json={"messages": messages, "stream": True, "temperature": 0.3},
                    ) as response,
                ):
                    if response.status_code < 400:
                        async for line in _iter_with_keepalive(response.aiter_lines()):
                            if line is _KEEPALIVE_SENTINEL:
                                yield ": keepalive\n\n"
                                continue
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            if delta.get("content"):
                                assistant_content += delta["content"]
                                yield f"data: {json.dumps({'type': 'token', 'content': delta['content']})}\n\n"
            except (httpx.ConnectError, httpx.TimeoutException):
                pass  # Fall through to the fallback message below

        # If we still have no text, emit a fallback message
        if not assistant_content:
            assistant_content = (
                "I used the available context to search but wasn't able to formulate a complete response. "
                "You can try rephrasing your question or asking something more specific. "
                "For broader questions that need to cover many documents, try the Research tab."
            )
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_content})}\n\n"

        # Estimate context usage for the UI indicator.
        # Include tool schema, all messages sent to the LLM, and the response.
        input_tokens = _estimate_messages_tokens(messages) + _get_tool_schema_tokens()
        response_tokens = _estimate_tokens(assistant_content) if assistant_content else 0
        used_tokens = input_tokens + response_tokens
        context_pct = round(min(used_tokens / context_tokens, 1.0) * 100) if context_tokens > 0 else 0

        # Save assistant response
        assistant_msg = None
        if assistant_content:
            assistant_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                tokens_used=total_tokens or None,
                rag_context=None,
                model_id=active_model_id,
                context_pct=context_pct,
            )
            session.add(assistant_msg)

        # Auto-title if this is the first exchange
        conv = await session.get(Conversation, conversation_id)
        if conv and conv.title == "New conversation":
            conv.title = _generate_title(user_message)

        await session.commit()

        done_payload: dict = {
            "type": "done",
            "message_id": str(assistant_msg.message_id) if assistant_msg else None,
            "context_pct": context_pct,
        }
        if conv and conv.title != "New conversation":
            done_payload["title"] = conv.title
        if active_model_id:
            done_payload["model_id"] = active_model_id
        yield f"data: {json.dumps(done_payload)}\n\n"


def _generate_title(user_message: str) -> str:
    """Generate a short title from the first user message."""
    title = user_message.strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return title


_LARGE_ARRAY_KEYS = ("documents", "results", "entities", "related", "passages", "chunks", "headings")


def _truncate_for_llm(result_str: str, max_chars: int = 28_000) -> str:
    """Truncate a tool result so it fits within the LLM context window.

    For JSON with large array fields, truncates the array and adds metadata.
    For non-JSON or unrecognised structure, hard-truncates the string.
    """
    if len(result_str) <= max_chars:
        return result_str

    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    if not isinstance(data, dict):
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    # Find the largest array field to truncate
    target_key = None
    target_len = 0
    for key in _LARGE_ARRAY_KEYS:
        if key in data and isinstance(data[key], list) and len(data[key]) > target_len:
            target_key = key
            target_len = len(data[key])

    if not target_key or target_len == 0:
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    # Binary-search for how many items fit
    original_array = data[target_key]
    original_count = len(original_array)
    data["_truncated"] = True
    data["_original_count"] = original_count
    lo, hi = 0, original_count
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data[target_key] = original_array[:mid]
        if len(json.dumps(data, ensure_ascii=False)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1

    data[target_key] = original_array[:lo] if lo > 0 else []
    result = json.dumps(data, ensure_ascii=False)
    if len(result) > max_chars:
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"
    return result


def _summarize_result(result_str: str) -> str:
    """Create a short summary of a tool result for the UI."""
    try:
        data = json.loads(result_str)
        if "error" in data:
            return f"Error: {data['error']}"
        if "hits" in data:
            return f"Found {len(data['hits'])} results"
        if "results" in data:
            return f"Found {data.get('count', len(data['results']))} results"
        if "passages" in data:
            return f"Read {len(data['passages'])} passages"
        if "chunks" in data:
            return f"Read {len(data['chunks'])} chunks"
        if "documents" in data:
            return f"{len(data['documents'])} documents"
        if "document" in data:
            return f"Document: {data['document'].get('title', 'Untitled')}"
        if "headings" in data:
            return f"{len(data.get('headings', []))} headings"
        if "related" in data:
            return f"{len(data['related'])} related documents"
        if "entities" in data:
            return f"{len(data['entities'])} entities"
        if "stages" in data:
            return f"Status: {data.get('overall_status', 'unknown')}"
        if "total_documents" in data:
            return f"{data['total_documents']} documents in corpus"
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return "Done"
