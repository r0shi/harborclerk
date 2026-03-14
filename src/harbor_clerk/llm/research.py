"""Research engine — multi-round iteration loop with scratchpad and synthesis pass."""

import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.models import get_model
from harbor_clerk.llm.tools import RESEARCH_TOOLS, execute_tool
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.document import Document
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt constants
# ---------------------------------------------------------------------------

_ITERATION_SYSTEM_SEARCH = (
    "You are a research assistant for Harbor Clerk. Your task is to systematically "
    "search the knowledge base to thoroughly answer the user's question.\n\n"
    "## How to work\n"
    "- Search broadly first, then drill into promising results\n"
    "- Use different search queries to cover different angles of the topic\n"
    "- Read passages to verify and gather detail from search hits\n"
    "- Use entity_search to find people, organizations, and places\n\n"
    "## Notes rules\n"
    "- Maintain your accumulated findings in a <notes> section at the end of "
    "every response\n"
    "- Every finding MUST include its source in brackets: [Document Title, page X]\n"
    "- Keep notes concise — summarize findings, don't copy full passages\n"
    "- When condensing notes, you may rephrase findings but NEVER remove citations\n"
    "- Citations are the most important part of your notes — the final report "
    "depends on them\n\n"
    "## Important\n"
    "- Do NOT call the same tool with the same arguments twice — vary your queries\n"
    "- If your notes are not growing, try different search terms or tools\n\n"
    "## Finishing\n"
    "When you are confident you have thoroughly covered the topic, stop calling "
    "tools and write ONLY a <report> tag. A separate synthesis step will produce "
    "the final report from your notes."
)

_ITERATION_SYSTEM_SWEEP = (
    "You are a research assistant for Harbor Clerk. Your task is to systematically "
    "review documents from the knowledge base to thoroughly answer the user's question.\n\n"
    "## How to work\n"
    "- Focus on the document batch provided each round\n"
    "- Search within those documents, read relevant passages, and extract findings\n"
    "- Not every document will be relevant — skip irrelevant ones quickly\n"
    "- Use entity_search to find people, organizations, and places\n\n"
    "## Notes rules\n"
    "- Maintain your accumulated findings in a <notes> section at the end of "
    "every response\n"
    "- Every finding MUST include its source in brackets: [Document Title, page X]\n"
    "- Keep notes concise — summarize findings, don't copy full passages\n"
    "- When condensing notes, you may rephrase findings but NEVER remove citations\n"
    "- Citations are the most important part of your notes — the final report "
    "depends on them\n\n"
    "## Important\n"
    "- Do NOT call the same tool with the same arguments twice — vary your queries\n"
    "- If your notes are not growing, try different search terms or tools\n\n"
    "## Finishing\n"
    "When you are confident you have thoroughly covered the topic, stop calling "
    "tools and write ONLY a <report> tag. A separate synthesis step will produce "
    "the final report from your notes."
)

_SWEEP_BATCH_PREFIX = (
    "## Current batch\n"
    "Focus on the following documents this round. Search within them, read relevant "
    "passages, and add any findings to your notes. Not every document will be "
    "relevant — skip irrelevant ones quickly.\n\n"
)

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

# Sweep batch size: how many docs to inject per round
_SWEEP_BATCH_SIZE = 5

# Timeouts
_ITERATION_TIMEOUT = 300.0
_SYNTHESIS_TIMEOUT = 600.0

# Rough chars-per-token estimate (same as chat.py)
_CHARS_PER_TOKEN = 3.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_notes(response: str) -> str | None:
    """Extract content between <notes>...</notes> tags from model response.

    Returns None if no notes block is found.
    """
    match = re.search(r"<notes>(.*?)</notes>", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _detect_report_signal(response: str) -> bool:
    """Check if the model emitted a <report> tag, signalling it is done iterating."""
    return "<report>" in response.lower()


def _build_iteration_messages(
    system_prompt: str,
    user_question: str,
    notes: str | None,
    strategy: str,
    sweep_batch_text: str | None = None,
) -> list[dict]:
    """Construct the message list for one iteration of the research loop."""
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # User question with notes context
    user_content = user_question
    if notes:
        user_content += f"\n\n<notes>\n{notes}\n</notes>"

    user_content += "\n\nContinue researching. Call tools to gather more findings, then update your <notes> block."

    if strategy == "sweep" and sweep_batch_text:
        user_content += f"\n\n{_SWEEP_BATCH_PREFIX}{sweep_batch_text}"

    messages.append({"role": "user", "content": user_content})
    return messages


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


def _summarize_tool_result(name: str, result_str: str) -> str:
    """Create a brief summary of a tool result for progress events."""
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
) -> AsyncGenerator[str, None]:
    """Stream research progress as SSE events.

    This is the main research engine. It runs an iteration loop where the LLM
    calls tools and accumulates findings in a scratchpad (<notes>), then does
    a fresh-context synthesis pass to produce the final report.

    Yields SSE-formatted strings (``data: {...}\\n\\n``).
    """
    settings = get_settings()
    active_model_id = settings.llm_model_id or None
    llm_url = f"{settings.llama_server_url}/v1/chat/completions"

    # Compute tool result truncation limit from model context window.
    model = get_model(settings.llm_model_id) if settings.llm_model_id else None
    if model and settings.llm_yarn_enabled and model.yarn:
        context_tokens = model.yarn.extended_context
    else:
        context_tokens = model.context_window if model else 32768
    tool_result_max_chars = min(int(context_tokens * 0.15 * _CHARS_PER_TOKEN), 24_000)

    async with async_session_factory() as session:
        # Load research state
        state = await session.get(ResearchState, conversation_id)
        if state is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Research state not found'})}\n\n"
            return

        # Load conversation and user question
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

        # Load per-model settings
        max_rounds = state.max_rounds
        strategy = state.strategy

        # Resume: load existing notes and current_round
        notes = state.notes or ""
        current_round = state.current_round if resume else 0

        # Mark as running
        state.status = "running"
        state.heartbeat_at = datetime.now(UTC)
        await session.commit()

        # Choose system prompt based on strategy
        system_prompt = _ITERATION_SYSTEM_SEARCH if strategy == "search" else _ITERATION_SYSTEM_SWEEP

        # For sweep strategy, fetch document list and compute batches
        doc_batches: list[list[dict]] = []
        total_docs = 0
        if strategy == "sweep":
            all_docs = await _fetch_document_list(user_id)
            total_docs = len(all_docs)
            for i in range(0, len(all_docs), _SWEEP_BATCH_SIZE):
                doc_batches.append(all_docs[i : i + _SWEEP_BATCH_SIZE])

        # Restore sweep batch index from progress on resume, or start at 0
        sweep_batch_idx = 0
        if resume and state.progress and "sweep_batch_idx" in state.progress:
            sweep_batch_idx = state.progress["sweep_batch_idx"]
        elif resume and strategy == "sweep":
            sweep_batch_idx = current_round  # fallback: assume 1:1 round/batch
        tools_called_total = 0
        prev_notes_len = 0
        stall_count = 0
        _STALL_ROUNDS = 3  # break to synthesis after this many rounds with unchanged notes

        # ---------------------------------------------------------------
        # Iteration loop
        # ---------------------------------------------------------------
        try:
            async with httpx.AsyncClient() as client:
                while current_round < max_rounds:
                    current_round += 1

                    # Build sweep batch text if applicable
                    sweep_batch_text = None
                    if strategy == "sweep" and doc_batches:
                        if sweep_batch_idx < len(doc_batches):
                            batch = doc_batches[sweep_batch_idx]
                            lines = [f"- {d['title']} (doc_id: {d['doc_id']})" for d in batch]
                            sweep_batch_text = "\n".join(lines)
                            sweep_batch_idx += 1
                        else:
                            # All batches covered — done iterating
                            break

                    # Yield progress event
                    progress_event: dict = {
                        "type": "progress",
                        "round": current_round,
                        "max_rounds": max_rounds,
                        "strategy": strategy,
                    }
                    if strategy == "sweep" and total_docs > 0:
                        reviewed = min(sweep_batch_idx * _SWEEP_BATCH_SIZE, total_docs)
                        progress_event["reviewed"] = reviewed
                        progress_event["total"] = total_docs
                    else:
                        progress_event["tools_called"] = tools_called_total
                    yield f"data: {json.dumps(progress_event)}\n\n"

                    # Build messages for this iteration
                    messages = _build_iteration_messages(
                        system_prompt,
                        user_question,
                        notes if notes else None,
                        strategy,
                        sweep_batch_text,
                    )

                    # Call LLM with tools
                    try:
                        assistant_content, tool_calls = await _stream_llm_call(
                            client,
                            llm_url,
                            messages,
                            tools=RESEARCH_TOOLS,
                            timeout=_ITERATION_TIMEOUT,
                        )
                    except httpx.HTTPStatusError as exc:
                        logger.error("LLM HTTP error during research iteration: %s", exc)
                        state.status = "interrupted"
                        state.current_round = current_round
                        state.error = f"LLM error: {exc.response.status_code}"
                        await session.commit()
                        yield f"data: {json.dumps({'type': 'error', 'message': f'LLM error ({exc.response.status_code})'})}\n\n"
                        return
                    except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                        logger.error("LLM connection error during research iteration: %s", exc)
                        state.status = "interrupted"
                        state.current_round = current_round
                        state.error = "LLM server not reachable"
                        await session.commit()
                        yield f"data: {json.dumps({'type': 'error', 'message': 'LLM server is not running. Select and activate a model in Settings.'})}\n\n"
                        return

                    # If model called tools, execute them
                    logger.debug(
                        "round=%d assistant_len=%d tool_calls=%d first_tc=%s",
                        current_round,
                        len(assistant_content),
                        len(tool_calls),
                        tool_calls[0]["function"]["name"] if tool_calls else "none",
                    )
                    has_tool_calls = bool(tool_calls and tool_calls[0]["function"]["name"])

                    if has_tool_calls:
                        # Save assistant tool-call message
                        tc_msg = ChatMessage(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=assistant_content or "",
                            tool_calls=tool_calls,
                            model_id=active_model_id,
                        )
                        session.add(tc_msg)

                        # Execute tools and collect results as plain text
                        tool_results_text = ""
                        for tc in tool_calls:
                            fn_name = tc["function"]["name"]
                            try:
                                fn_args = json.loads(tc["function"]["arguments"])
                            except json.JSONDecodeError:
                                fn_args = {}

                            yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'arguments': fn_args})}\n\n"

                            result_str = await execute_tool(fn_name, fn_args, user_id, mode="research")
                            tools_called_total += 1
                            logger.debug(
                                "tool=%s args=%s result_len=%d result_start=%.200s",
                                fn_name,
                                fn_args,
                                len(result_str),
                                result_str,
                            )

                            summary = _summarize_tool_result(fn_name, result_str)
                            yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summary})}\n\n"

                            # Save full tool result as message
                            tool_msg = ChatMessage(
                                conversation_id=conversation_id,
                                role="tool",
                                content=result_str,
                                tool_call_id=tc.get("id", f"call_{fn_name}"),
                            )
                            session.add(tool_msg)

                            # Accumulate truncated results as plain text for follow-up
                            truncated = _truncate_for_context(result_str, tool_result_max_chars)
                            tool_results_text += (
                                f"## {fn_name}({', '.join(f'{k}={v!r}' for k, v in fn_args.items())})\n{truncated}\n\n"
                            )

                        await session.flush()

                        # Follow-up LLM call with tool results as plain text
                        # (avoids tool_calls/tool message format which requires
                        # tools in payload for llama-server to template correctly)
                        followup_messages = list(messages)
                        followup_messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Here are the results from your tool calls:\n\n"
                                    f"{tool_results_text}"
                                    "Review these results and update your <notes> with any new findings. "
                                    "Include citations as [Document Title, page X] for every finding."
                                ),
                            }
                        )

                        try:
                            logger.debug(
                                "follow-up call: %d messages, total_chars=%d",
                                len(followup_messages),
                                sum(len(str(m.get("content", ""))) for m in followup_messages),
                            )
                            followup_content, _ = await _stream_llm_call(
                                client,
                                llm_url,
                                followup_messages,
                                tools=None,
                                timeout=_ITERATION_TIMEOUT,
                            )
                            logger.debug("follow-up OK: len=%d preview=%.300s", len(followup_content), followup_content)
                        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as exc:
                            logger.error("LLM error during follow-up: %s", exc)
                            # Use whatever notes we have so far
                            followup_content = assistant_content

                        # Save follow-up as assistant message
                        if followup_content:
                            followup_msg = ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=followup_content,
                                model_id=active_model_id,
                            )
                            session.add(followup_msg)

                        # Parse notes from follow-up (or fallback to original response)
                        new_notes = _parse_notes(followup_content) if followup_content else None
                        if new_notes is None:
                            new_notes = _parse_notes(assistant_content) if assistant_content else None
                        logger.debug("notes_parsed=%s notes_len=%d", new_notes is not None, len(new_notes or ""))
                        if new_notes is not None:
                            notes = new_notes

                        # Check if model signalled completion
                        if _detect_report_signal(followup_content or "") or _detect_report_signal(
                            assistant_content or ""
                        ):
                            # Checkpoint and break to synthesis
                            state.notes = notes
                            state.current_round = current_round
                            state.heartbeat_at = datetime.now(UTC)
                            state.progress = {"tools_called": tools_called_total}
                            await session.commit()
                            break

                    else:
                        # No tool calls — model is done or produced text only
                        if assistant_content:
                            msg = ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=assistant_content,
                                model_id=active_model_id,
                            )
                            session.add(msg)

                        # Parse notes from the response
                        new_notes = _parse_notes(assistant_content) if assistant_content else None
                        if new_notes is not None:
                            notes = new_notes

                        # No tools called → done iterating
                        state.notes = notes
                        state.current_round = current_round
                        state.heartbeat_at = datetime.now(UTC)
                        state.progress = {"tools_called": tools_called_total}
                        await session.commit()
                        break

                    # Detect stall: if notes haven't grown, the model is stuck
                    cur_notes_len = len(notes)
                    if cur_notes_len > 0 and cur_notes_len == prev_notes_len:
                        stall_count += 1
                        if stall_count >= _STALL_ROUNDS:
                            logger.info(
                                "Research stalled (%d rounds, notes=%d chars) — moving to synthesis",
                                stall_count,
                                cur_notes_len,
                            )
                            state.notes = notes
                            state.current_round = current_round
                            state.heartbeat_at = datetime.now(UTC)
                            state.progress = {"tools_called": tools_called_total}
                            await session.commit()
                            break
                    else:
                        stall_count = 0
                    prev_notes_len = cur_notes_len

                    # Checkpoint notes after each iteration
                    state.notes = notes
                    state.current_round = current_round
                    state.heartbeat_at = datetime.now(UTC)
                    if strategy == "sweep":
                        state.progress = {
                            "reviewed": min(sweep_batch_idx * _SWEEP_BATCH_SIZE, total_docs),
                            "total": total_docs,
                            "sweep_batch_idx": sweep_batch_idx,
                        }
                    else:
                        state.progress = {"tools_called": tools_called_total}
                    await session.commit()

                # ---------------------------------------------------------------
                # Synthesis pass
                # ---------------------------------------------------------------
                if not notes:
                    # No findings at all — report that
                    notes = "No relevant findings were discovered during the research."

                yield f"data: {json.dumps({'type': 'synthesis', 'status': 'started'})}\n\n"

                synthesis_messages = _build_synthesis_messages(user_question, notes)
                report_content = ""

                try:
                    async for token in _stream_llm_tokens(
                        client,
                        llm_url,
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
                except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                    logger.error("LLM connection error during synthesis: %s", exc)
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
                state.current_round = current_round
                state.completed_at = datetime.now(UTC)
                if strategy == "sweep":
                    state.progress = {
                        "reviewed": min(sweep_batch_idx * _SWEEP_BATCH_SIZE, total_docs),
                        "total": total_docs,
                    }
                else:
                    state.progress = {"tools_called": tools_called_total}

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
            state.current_round = current_round
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
                    state.current_round = current_round
                    await session.commit()
            except Exception:
                logger.exception("Failed to mark research as interrupted on disconnect")
