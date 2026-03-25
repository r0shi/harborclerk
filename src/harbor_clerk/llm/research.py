"""Research engine — smolagents ToolCallingAgent with synthesis pass."""

import asyncio
import json
import logging
import queue as queue_mod
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.tools import summarize_tool_result
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.document import Document
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt constants
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are writing a research report for Harbor Clerk. Based on the research "
    "notes below, write a clear, well-organized report answering the user's question.\n\n"
    "## Guidelines\n"
    "- Every claim must cite a source using this format: [Document Title, page X]\n"
    "- If a finding has no citation, omit it\n"
    "- Group findings by theme, not by document\n"
    "- Be thorough but concise — include all relevant findings, skip filler\n"
    "- If the evidence is contradictory or incomplete, say so\n"
    "- Do not invent information not present in the notes"
)

# Timeouts
_ITERATION_TIMEOUT = 300.0
_SYNTHESIS_TIMEOUT = 600.0
_KEEPALIVE_INTERVAL = 30.0  # SSE keepalive + heartbeat during LLM calls

# Rough chars-per-token estimate (same as chat.py)
_CHARS_PER_TOKEN = 3.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_synthesis_messages(user_question: str, notes: str) -> list[dict]:
    """Construct messages for the final synthesis/report pass."""
    return [
        {"role": "system", "content": _SYNTHESIS_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Original question\n{user_question}\n\n"
                f"## Research notes\n<notes>\n{notes}\n</notes>\n\n"
                "Write your final report with citations."
            ),
        },
    ]


async def _fetch_document_list(user_id: uuid.UUID | None) -> list[dict]:
    """Get corpus document list for sweep strategy.

    Returns a list of dicts with doc_id, title for batching.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Document.doc_id, Document.title).where(Document.status == "ready").order_by(Document.title)
        )
        return [{"doc_id": str(row.doc_id), "title": row.title} for row in result.all()]


def _truncate_for_context(text: str, max_chars: int) -> str:
    """Truncate text for LLM context if it exceeds max_chars."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated — {len(text)} chars total]"


async def _stream_llm_call(
    client: httpx.AsyncClient,
    url: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    timeout: float = _ITERATION_TIMEOUT,
) -> tuple[str, list[dict]]:
    """Make a streaming LLM call, accumulate text and tool calls.

    Returns (assistant_content, tool_calls_accumulated).
    """
    payload: dict = {
        "messages": messages,
        "stream": True,
        "temperature": 0.3,
    }
    if tools:
        payload["tools"] = tools

    assistant_content = ""
    tool_calls_accumulated: list[dict] = []

    async with client.stream(
        "POST",
        url,
        json=payload,
        timeout=httpx.Timeout(timeout),
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            # Accumulate text content
            if delta.get("content"):
                assistant_content += delta["content"]

            # Accumulate tool calls (index-based)
            if delta.get("tool_calls"):
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    while len(tool_calls_accumulated) <= idx:
                        tool_calls_accumulated.append(
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        )
                    if tc.get("id"):
                        tool_calls_accumulated[idx]["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        tool_calls_accumulated[idx]["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tool_calls_accumulated[idx]["function"]["arguments"] += fn["arguments"]

    return assistant_content, tool_calls_accumulated


async def _stream_llm_tokens(
    client: httpx.AsyncClient,
    url: str,
    messages: list[dict],
    *,
    timeout: float = _SYNTHESIS_TIMEOUT,
) -> AsyncGenerator[str, None]:
    """Stream LLM response tokens (no tool calling). Yields text chunks."""
    payload: dict = {
        "messages": messages,
        "stream": True,
        "temperature": 0.3,
    }

    async with client.stream(
        "POST",
        url,
        json=payload,
        timeout=httpx.Timeout(timeout),
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                yield delta["content"]


# ---------------------------------------------------------------------------
# Main research engine
# ---------------------------------------------------------------------------


async def research_stream(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    resume: bool = False,
    depth: str = "standard",
) -> AsyncGenerator[str, None]:
    """Stream research progress as SSE events using smolagents ToolCallingAgent.

    The agent iterates with tool calls, then a fresh-context synthesis pass
    produces the final cited report.

    Yields SSE-formatted strings (``data: {...}\\n\\n``).
    """
    settings = get_settings()
    active_model_id = settings.llm_model_id or None
    llm_url = f"{settings.llama_server_url}/v1"

    async with async_session_factory() as session:
        # Load research state
        state = await session.get(ResearchState, conversation_id)
        if state is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Research state not found'})}\n\n"
            return

        # Load conversation
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Conversation not found'})}\n\n"
            return

        # Get the original user question (first user message)
        q_result = await session.execute(
            select(ChatMessage.content)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.role == "user")
            .order_by(ChatMessage.created_at)
            .limit(1)
        )
        user_question = q_result.scalar_one_or_none()
        if not user_question:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No user question found'})}\n\n"
            return

        strategy = state.strategy

        # Mark as running
        state.status = "running"
        state.heartbeat_at = datetime.now(UTC)
        await session.commit()

        # Suppress extremely verbose openai/httpx debug logging from smolagents
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)

        # Configure smolagents agent
        from smolagents import (
            ActionOutput,
            ActionStep,
            ChatMessageStreamDelta,
            FinalAnswerStep,
            OpenAIServerModel,
            PlanningStep,
            ToolCall,
            ToolCallingAgent,
            ToolOutput,
        )

        from harbor_clerk.llm.research_tools import build_research_tools

        model = OpenAIServerModel(
            model_id="local",
            api_base=llm_url,
            api_key="not-needed",
        )
        tools = build_research_tools(user_id, main_loop=asyncio.get_running_loop())

        planning_map = {"light": 3, "standard": 5, "thorough": 10}
        planning_interval = planning_map.get(depth, 5)
        time_limit_s = (state.time_limit_minutes or 30) * 60
        max_steps = max(10, time_limit_s // 30)

        agent = ToolCallingAgent(
            tools=tools,
            model=model,
            planning_interval=planning_interval,
        )

        # Build task description
        task = user_question
        if strategy == "sweep":
            doc_list = await _fetch_document_list(user_id)
            if doc_list:
                doc_text = "\n".join(f"- {d['title']} (doc_id: {d['doc_id']})" for d in doc_list)
                task += f"\n\nHere are all documents in the corpus:\n{doc_text}\n\nReview these systematically."

        start_time = datetime.now(UTC)
        final_answer_text = ""
        step_count = 0

        try:
            # Run agent in thread, stream steps via queue
            step_queue: queue_mod.Queue = queue_mod.Queue()

            def _run_agent():
                try:
                    logger.info("Agent thread starting: task=%s max_steps=%d", task[:100], max_steps)
                    for step in agent.run(task=task, stream=True, max_steps=max_steps):
                        step_queue.put(("step", step))
                        logger.debug("Agent step: %s", type(step).__name__)
                        # Wall-time check
                        elapsed = (datetime.now(UTC) - start_time).total_seconds()
                        if elapsed >= time_limit_s:
                            logger.info("Research time limit reached in agent thread")
                            break
                except Exception as exc:
                    logger.error("Agent thread error: %s: %s", type(exc).__name__, exc, exc_info=True)
                    step_queue.put(("error", exc))
                finally:
                    step_queue.put(("done", None))

            loop = asyncio.get_running_loop()
            executor_future = loop.run_in_executor(None, _run_agent)

            while True:
                try:
                    msg = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: step_queue.get(timeout=30)),
                        timeout=35,
                    )
                except Exception:
                    # Keepalive
                    state.heartbeat_at = datetime.now(UTC)
                    await session.commit()
                    yield ": keepalive\n\n"
                    continue

                msg_type, msg_data = msg
                logger.debug(
                    "Queue message: type=%s data_type=%s", msg_type, type(msg_data).__name__ if msg_data else None
                )

                if msg_type == "done":
                    break

                if msg_type == "error":
                    logger.error("Agent error: %s", msg_data)
                    yield f"data: {json.dumps({'type': 'error', 'message': str(msg_data)})}\n\n"
                    break

                step = msg_data

                if isinstance(step, ToolCall):
                    tc_args = step.arguments if isinstance(step.arguments, dict) else {}
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': step.name, 'arguments': tc_args})}\n\n"
                    # Update heartbeat
                    state.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                elif isinstance(step, ToolOutput):
                    summary = summarize_tool_result(str(step.observation or step.output)[:500])
                    tc_name = step.tool_call.name if step.tool_call else "unknown"
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': tc_name, 'summary': summary})}\n\n"
                    if step.is_final_answer:
                        final_answer_text = str(step.output)

                elif isinstance(step, ActionOutput):
                    if step.is_final_answer and step.output:
                        final_answer_text = str(step.output)

                elif isinstance(step, ChatMessageStreamDelta):
                    if step.content:
                        yield f"data: {json.dumps({'type': 'notes', 'content': step.content})}\n\n"

                elif isinstance(step, PlanningStep):
                    if step.plan:
                        yield f"data: {json.dumps({'type': 'notes', 'content': f'Planning: {step.plan[:500]}'})}\n\n"

                elif isinstance(step, ActionStep):
                    step_count = step.step_number
                    elapsed = int((datetime.now(UTC) - start_time).total_seconds())

                    progress_event = {
                        "type": "progress",
                        "step": step_count,
                        "elapsed_seconds": elapsed,
                        "time_limit_minutes": state.time_limit_minutes or 30,
                        "strategy": strategy,
                    }
                    yield f"data: {json.dumps(progress_event)}\n\n"

                    # Emit agent's thinking from this step
                    if step.model_output:
                        model_text = step.model_output if isinstance(step.model_output, str) else str(step.model_output)
                        if model_text.strip():
                            yield f"data: {json.dumps({'type': 'notes', 'content': model_text[:2000]})}\n\n"

                    if step.is_final_answer and step.action_output:
                        final_answer_text = str(step.action_output)

                    # Checkpoint
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    state.progress = {"step": step_count}
                    await session.commit()

                elif isinstance(step, FinalAnswerStep):
                    if hasattr(step, "output") and step.output:
                        final_answer_text = str(step.output)

            # Wait for executor to finish
            try:
                await asyncio.wait_for(asyncio.shield(executor_future), timeout=10)
            except Exception:
                pass

            # If no final answer from agent, try to extract from memory
            if not final_answer_text and hasattr(agent, "memory") and agent.memory:
                try:
                    for mem_step in reversed(agent.memory.steps):
                        if hasattr(mem_step, "action_output") and mem_step.action_output:
                            final_answer_text = str(mem_step.action_output)
                            break
                        if hasattr(mem_step, "model_output") and mem_step.model_output:
                            text = (
                                mem_step.model_output
                                if isinstance(mem_step.model_output, str)
                                else str(mem_step.model_output)
                            )
                            if len(text) > len(final_answer_text):
                                final_answer_text = text
                except Exception:
                    pass

            # Save agent's final answer as a message
            if final_answer_text:
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=final_answer_text,
                        model_id=active_model_id,
                    )
                )
                await session.flush()

            # ---------------------------------------------------------------
            # Synthesis pass
            # ---------------------------------------------------------------
            notes = final_answer_text or "No relevant findings were discovered during the research."

            yield f"data: {json.dumps({'type': 'synthesis', 'status': 'started'})}\n\n"

            synthesis_messages = _build_synthesis_messages(user_question, notes)
            report_content = ""
            synthesis_url = f"{settings.llama_server_url}/v1/chat/completions"

            try:
                async with httpx.AsyncClient() as client:
                    async for token in _stream_llm_tokens(
                        client,
                        synthesis_url,
                        synthesis_messages,
                        timeout=_SYNTHESIS_TIMEOUT,
                    ):
                        report_content += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            except httpx.HTTPStatusError as exc:
                logger.error("LLM HTTP error during synthesis: %s", exc)
                state.status = "interrupted"
                state.error = f"Synthesis failed: LLM error ({exc.response.status_code})"
                await session.commit()
                yield f"data: {json.dumps({'type': 'error', 'message': f'Synthesis failed: LLM error ({exc.response.status_code})'})}\n\n"
                return
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                logger.error("LLM connection/timeout error during synthesis: %s", exc)
                state.status = "interrupted"
                state.error = "Synthesis failed: LLM server not reachable"
                await session.commit()
                yield f"data: {json.dumps({'type': 'error', 'message': 'LLM server is not running. Select and activate a model in Settings.'})}\n\n"
                return

            # Save report as assistant message
            report_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=report_content,
                model_id=active_model_id,
            )
            session.add(report_msg)

            # Finalize research state
            state.status = "completed"
            state.notes = notes
            state.current_round = step_count
            state.completed_at = datetime.now(UTC)
            state.progress = {"step": step_count}

            await session.commit()

            done_payload: dict = {
                "type": "done",
                "conversation_id": str(conversation_id),
            }
            if active_model_id:
                done_payload["model_id"] = active_model_id
            yield f"data: {json.dumps(done_payload)}\n\n"

        except Exception:
            logger.exception("Unexpected error in research_stream (conversation=%s)", conversation_id)
            state.status = "failed"
            state.error = "Unexpected internal error"
            state.current_round = step_count
            try:
                await session.commit()
            except Exception:
                logger.exception("Failed to save error state")
            yield f"data: {json.dumps({'type': 'error', 'message': 'An unexpected error occurred during research.'})}\n\n"

        finally:
            # If still running when generator exits (client disconnect, cancel),
            # mark as interrupted so it doesn't block future research/chat.
            try:
                await session.refresh(state)
                if state.status == "running":
                    logger.info("Research stream disconnected, marking interrupted (conversation=%s)", conversation_id)
                    state.status = "interrupted"
                    state.current_round = step_count
                    await session.commit()
            except Exception:
                logger.exception("Failed to mark research as interrupted on disconnect")
