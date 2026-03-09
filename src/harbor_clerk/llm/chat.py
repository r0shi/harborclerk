"""Chat orchestration — streaming tool-calling loop against llama-server."""

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
    "For factual questions about document content:\n"
    "  search → read passages → expand context if needed\n\n"
    "For structural or exploratory questions "
    '("what\'s in the corpus?", "list documents about X"):\n'
    "  corpus_overview, list_documents, or document_outline\n\n"
    'For entity questions ("who is mentioned?", "what organizations?"):\n'
    "  entity_search, entity_overview, or entity_cooccurrence\n\n"
    "For document comparison or discovery:\n"
    "  find_related\n\n"
    "For processing status:\n"
    "  ingest_status\n\n"
    "When a query is broad or ambiguous, start with corpus_overview or "
    "list_documents to understand what is available before searching.\n\n"
    "## Language\n\n"
    "Respond in the same language as the user's question."
)

SYSTEM_PROMPT = _CORE_INSTRUCTIONS


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

        # Tool-calling loop
        assistant_content = ""
        total_tokens = 0

        for _round in range(settings.max_tool_rounds):
            tool_calls_accumulated: list[dict] = []
            text_buffer = ""

            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client,
                    client.stream(
                        "POST",
                        f"{settings.llama_server_url}/v1/chat/completions",
                        json={
                            "messages": messages,
                            "tools": CHAT_TOOLS,
                            "stream": True,
                            "temperature": 0.3,
                        },
                    ) as response,
                ):
                    if response.status_code >= 400:
                        await response.aread()
                        detail = response.text[:2000]
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
                        done_payload: dict = {"type": "done"}
                        if conv and conv.title != "New conversation":
                            done_payload["title"] = conv.title
                        if active_model_id:
                            done_payload["model_id"] = active_model_id
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return

                    async for line in response.aiter_lines():
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

            except (httpx.ConnectError, httpx.ReadTimeout):
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

        # If the loop exhausted max_tool_rounds without a text response,
        # synthesize a fallback so the user sees what happened.
        if not assistant_content and _round == settings.max_tool_rounds - 1:
            logger.warning(
                "Chat loop exhausted %d tool rounds without text response (conversation=%s)",
                settings.max_tool_rounds,
                conversation_id,
            )
            assistant_content = (
                "I used all available tool calls but wasn't able to formulate a complete response. "
                "You can try rephrasing your question or asking something more specific."
            )
            yield f"data: {json.dumps({'type': 'token', 'content': assistant_content})}\n\n"

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
