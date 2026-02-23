"""Chat orchestration — streaming tool-calling loop against llama-server."""

import json
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.llm.tools import CHAT_TOOLS, execute_tool
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Harbor Clerk, a document assistant for a local knowledge base. "
    "Use the search_documents tool to find relevant information before answering. "
    "Always cite your sources with document titles and page numbers. "
    "If you cannot find relevant information, say so honestly. "
    "Respond in the same language as the user's question. Be concise and factual."
)

MAX_TOOL_ROUNDS = 5
MAX_HISTORY_MESSAGES = 40


async def chat_stream(
    conversation_id: uuid.UUID,
    user_message: str,
    session: AsyncSession,
) -> AsyncGenerator[str, None]:
    """Stream chat response as SSE events. Handles tool-calling loop internally."""
    settings = get_settings()

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
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at)
    )
    history_rows = history_result.scalars().all()

    # Build messages for the LLM
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
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

    # Tool-calling loop
    assistant_content = ""
    total_tokens = 0

    for _round in range(MAX_TOOL_ROUNDS):
        tool_calls_accumulated: list[dict] = []
        text_buffer = ""

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                async with client.stream(
                    "POST",
                    f"{settings.llama_server_url}/v1/chat/completions",
                    json={
                        "messages": messages,
                        "tools": CHAT_TOOLS,
                        "stream": True,
                        "temperature": 0.3,
                    },
                ) as response:
                    response.raise_for_status()
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
                                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
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

        except httpx.HTTPStatusError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'LLM server error: {e.response.status_code}'})}\n\n"
            return
        except (httpx.ConnectError, httpx.ReadTimeout):
            yield f"data: {json.dumps({'type': 'error', 'message': 'LLM server unavailable'})}\n\n"
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
            messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls_accumulated})

            for tc in tool_calls_accumulated:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'arguments': fn_args})}\n\n"

                result_str = await execute_tool(fn_name, fn_args, session)

                yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': _summarize_result(result_str)})}\n\n"

                # Save tool result message
                tool_msg = ChatMessage(
                    conversation_id=conversation_id,
                    role="tool",
                    content=result_str,
                    tool_call_id=tc.get("id", f"call_{fn_name}"),
                )
                session.add(tool_msg)
                messages.append({
                    "role": "tool",
                    "content": result_str,
                    "tool_call_id": tc.get("id", f"call_{fn_name}"),
                })

            await session.flush()
            continue  # Next round — let LLM see tool results

        # No tool calls — we have the final text response
        assistant_content = text_buffer
        break

    # Save assistant response
    if assistant_content:
        assistant_msg = ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_content,
            tokens_used=total_tokens or None,
        )
        session.add(assistant_msg)

    # Auto-title if this is the first exchange
    conv = await session.get(Conversation, conversation_id)
    if conv and conv.title == "New conversation":
        conv.title = _generate_title(user_message)

    await session.commit()

    yield f"data: {json.dumps({'type': 'done', 'message_id': str(user_msg.message_id) if assistant_content else None})}\n\n"


def _generate_title(user_message: str) -> str:
    """Generate a short title from the first user message."""
    title = user_message.strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return title


def _summarize_result(result_str: str) -> str:
    """Create a short summary of a tool result for the UI."""
    try:
        data = json.loads(result_str)
        if "results" in data:
            return f"Found {data.get('count', len(data['results']))} results"
        if "passages" in data:
            return f"Read {len(data['passages'])} passages"
        if "error" in data:
            return f"Error: {data['error']}"
    except json.JSONDecodeError:
        pass
    return "Done"
