"""Chat orchestration — streaming tool-calling loop against llama-server."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from harbor_clerk.api.scope import build_user_scope
from harbor_clerk.config import get_settings, refresh_llm_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.citations import dedupe_citations, extract_citations_from_tool_result
from harbor_clerk.llm.health import report_llm_error, report_llm_success
from harbor_clerk.llm.models import context_budget, get_model
from harbor_clerk.llm.tools import execute_tool, get_chat_tools, summarize_tool_result
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

if TYPE_CHECKING:
    from harbor_clerk.llm.models import ModelInfo

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

# When a time budget (settings.chat_time_budget_seconds; 0 turns every bound off) is configured and spent, the
# forced final answer gets this long, from its first content token. Thinking is not content: a model that reasons
# before it writes spent the whole grace on reasoning deltas and the user got nothing (#742), so the forced call asks
# it not to think, and one that thinks regardless gets _FINAL_ANSWER_THINKING_SECONDS from its first token to start
# writing, or is cut: thinking alone cannot hold the answer open. What it thought by then is shown (see the forced
# call), so a model whose answer lives in its reasoning channel (gpt-oss) has this long for the whole of it.
_FINAL_ANSWER_GRACE_SECONDS = 120.0
_FINAL_ANSWER_THINKING_SECONDS = 120.0

# The forced final call's last message. Omitting the tool definitions says nothing to a model in the middle of a
# search: Qwen3.6 wrote the next tool call as text, eight times in ten (#742). Told the search is over, it answers.
# The second form is for a search cut before any result came back (the first generation outlasted the budget):
# there are no "results above" to answer from.
_SEARCH_OVER_MESSAGE = (
    "The search has ended. Answer the question now from the results above; do not call any tool. "
    "If they do not contain the answer, say what was found and what was not."
)
_SEARCH_OVER_UNSEARCHED_MESSAGE = (
    "The search has ended before any result came back; do not call any tool. Say that the documents could not be "
    "searched in the time allowed, and answer only what you can without them."
)

# The clock the budget is kept by; tests replace it here, not in ``time`` (the event loop keeps time there too).
_now = time.monotonic


def _stopped_early_note(spent_seconds: float) -> str:
    minutes = max(1, round(spent_seconds / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"\n\n_(I stopped after {minutes} {unit} and answered from what I had found.)_"


def _nothing_produced_fallback(stop_reason: str | None, server_failure: str | None = None) -> str:
    """The app's words when no answer came from the model, naming why (#742, #712 item 2, #684 item 3).

    The old sentence ("I used the available context to search but wasn't able to formulate a complete response")
    read as the model's answer, and a run of dead-server and budget failures was reported as answered. This one
    says who failed and in what: the model, which wrote nothing, or the model server, which failed the forced call
    (``server_failure`` says how). The eval harness recognises either opening (``APP_FALLBACK_LEADS`` in
    ``scripts/test_corpora/runner/quality.py``) and does not judge it as an answer: the first words are a contract.
    """
    stopped = {
        "time_budget": "the search stopped at its time budget",
        "context_budget": "the search results filled the model's context",
        "tool_rounds": f"the search stopped at its cap of {_MAX_TOOL_ROUNDS} tool rounds",
    }.get(stop_reason or "")
    if server_failure:
        lead = f"The model server failed while the final answer was being written ({server_failure})"
        lead += f": {stopped}, and the answer then asked for from what it had found did not come." if stopped else "."
        advice = "Try again in a moment; if it fails again, check the model in Settings."
    else:
        lead = {
            "time_budget": (
                "The model produced no answer in the time allowed: the search stopped at its time budget, and when "
                "asked to answer from what it had found, the model wrote nothing in the time it had."
            ),
            "context_budget": (
                "The model produced no answer in the context allowed: the search results filled the model's "
                "context, and when asked to answer from what it had found, the model wrote nothing."
            ),
            "tool_rounds": (
                f"The model produced no answer after {_MAX_TOOL_ROUNDS} tool rounds: the search stopped at its cap, "
                "and when asked to answer from what it had found, the model wrote nothing."
            ),
        }.get(stop_reason or "", "The model produced no answer: it returned nothing for this question.")
        advice = "Try a narrower question, or the Research tab for a question that spans many documents."
    return f"_{lead} {advice}_"


def _cut_thought_answer(thought: str, ran_out: str) -> str:
    """A thought cut short (by the time bounds, or by the server when the context ran out under it), under the app's
    lead: what the model had is shown, and not as an answer it gave. The lead's first words are the harness's too
    (``APP_FALLBACK_LEADS``)."""
    return f"_The model was still reasoning when its {ran_out} ran out; what it had:_\n\n{thought.strip()}"


def _forced_answer_cut(now: float, thinking_deadline: float | None, final_deadline: float | None) -> str | None:
    """Why the forced answer is cut at ``now`` ("grace" or "thinking"), or None to keep reading.

    The grace, once running, is the only bound. Before it runs, the thinking bound is, once the model's first
    token has started it; before that nothing is: the prompt is still being re-read.
    """
    if final_deadline is not None:
        return "grace" if now >= final_deadline else None
    if thinking_deadline is not None and now >= thinking_deadline:
        return "thinking"
    return None


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


def _get_tool_schema_tokens(model: "ModelInfo | None" = None) -> int:
    """Compute the tool schema token estimate from current settings."""
    return _estimate_tokens(json.dumps(get_chat_tools(model=model)))


def _context_usage(messages: list[dict], context_window: int, model: "ModelInfo | None" = None) -> float:
    """Return fraction of context window used by current messages + tool schema."""
    if context_window <= 0:
        return 0.0
    tokens = _estimate_messages_tokens(messages) + _get_tool_schema_tokens(model)
    return tokens / context_window


def _trim_to_budget(messages: list[dict], context_window: int, model: "ModelInfo | None" = None) -> list[dict]:
    """Trim oldest history messages to fit within context budget.

    Keeps the system prompt (index 0) and the most recent user message
    (last entry). Removes messages from the front of history, keeping
    tool call/result pairs together.

    Returns a new list (does not mutate the input).
    """
    budget = int(context_window * (1 - _RESPONSE_RESERVE))
    budget -= _get_tool_schema_tokens(model)

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
    "It finds relevant passages across the entire corpus, even with hundreds of documents. "
    "The one exception: a question about the earliest, latest, first or last document starts with "
    "documents_by_date (below), because similarity ranking does not find an edge.\n\n"
    "**Follow-up questions need new searches.** When the user asks about a "
    "specific topic, entity, or region not covered in your previous response, "
    "always search for it — do not assume it is absent from the corpus just "
    "because it was not in earlier results. Previous searches may not have "
    "covered all relevant content.\n\n"
    "For factual questions, including ones that name a document:\n"
    "  search_documents → read_passages → expand_context if needed\n"
    "  (verify_identifier only to tell apart similarly named documents in the results)\n\n"
    'For broad or comparative questions ("compare all X", "what does the corpus say about Y"):\n'
    "  Start with corpus_overview to see what documents and topics exist, then use\n"
    "  search_documents with targeted queries based on what you find.\n"
    "  Check has_more and total_candidates in results — if many candidates exist,\n"
    "  use offset to page through or try more specific queries to find what you missed.\n\n"
    'For structural questions ("how many documents?", "what file types?"):\n'
    "  corpus_overview\n\n"
    'For chronological questions ("the earliest email about X", "the last document before Y", "the most recent"):\n'
    "  documents_by_date, with the direction and the subject — search_documents ranks by similarity and\n"
    "  will not find an edge reliably\n\n"
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
    nxt: asyncio.Future | None = None
    try:
        while True:
            try:
                nxt = asyncio.ensure_future(ait.__anext__())
                # Wait for the next item, yielding keepalives while it's pending.
                # Re-use the SAME future across timeouts — never call __anext__ twice.
                while True:
                    done, _ = await asyncio.wait({nxt}, timeout=_KEEPALIVE_INTERVAL)
                    if done:
                        yield nxt.result()
                        break
                    yield _KEEPALIVE_SENTINEL
            except StopAsyncIteration:
                return
    finally:
        # A consumer that stops between items (the time budget, a disconnect) leaves the fetch pending; once
        # the response closes it would fail with nobody awaiting it ("Task exception was never retrieved").
        if nxt is not None and not nxt.done():
            nxt.cancel()


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
    # Pick up model changes that happened outside this process (typically
    # the menubar Preferences window writing config.json directly). Without
    # this, chat would stream against whatever model the API server cached
    # at startup — possibly a model llama-server is no longer hosting.
    refresh_llm_settings()
    settings = get_settings()
    active_model_id = settings.llm_model_id or None

    # Compute per-tool-result truncation limit from model context window.
    # Reserve ~25% of context for tool results (rest: system prompt, tools, history, response).
    # ~3.5 chars per token as a conservative estimate.
    # The context llama-server is running with, not the registry's: the
    # macOS launcher clamps -c to what fits this Mac (#556).
    model = get_model(settings.llm_model_id) if settings.llm_model_id else None
    context_tokens = context_budget(model, settings.llm_yarn_enabled)
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
        # Build scope filter from the conversation's stored scope JSONB.
        # user_scope is None when no folder restriction is active.
        user_scope = build_user_scope(conv.scope) if conv else None
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
        from harbor_clerk.topics import get_topic_summary

        topic_hint = await get_topic_summary()
        system_content = SYSTEM_PROMPT
        if topic_hint:
            system_content += f"\n\n## Corpus topics\n{topic_hint}"
        messages: list[dict] = [{"role": "system", "content": system_content}]
        for msg in history_rows[-settings.max_history_messages :]:
            content = _truncate_for_llm(msg.content, tool_result_max_chars) if msg.role == "tool" else msg.content
            entry: dict = {"role": msg.role, "content": content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
                if not msg.content:
                    entry["content"] = ""
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)

        # Trim history to fit within context budget
        messages = _trim_to_budget(messages, context_tokens, model)

        # Tool-calling loop — runs until the model produces a text response,
        # context budget is exhausted, or we hit the hard safety cap.
        assistant_content = ""
        total_tokens = 0
        budget_exhausted = False
        # The time budget (#719): checked between rounds and between streamed lines, so a single long
        # generation is cut too. 0 means no bound.
        time_budget = float(settings.chat_time_budget_seconds or 0)
        started_at = _now()
        deadline = started_at + time_budget if time_budget > 0 else None
        time_exhausted = False
        streamed_prefix = ""  # text the user watched arrive before a cut dropped the tool call beside it
        searched_this_turn = False  # a tool result came back in this turn (history may hold earlier turns' results)

        # Accumulated citations parsed from each tool result. Deduped+attached
        # to the assistant ChatMessage row (``rag_context`` column) and emitted
        # on the ``done`` SSE event so the UI and the test harness can surface
        # what the model cited without re-parsing the prose.
        citations_accumulated: list[dict] = []

        for _round in range(_MAX_TOOL_ROUNDS):
            # Check context budget before each LLM call
            usage_frac = _context_usage(messages, context_tokens, model)
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
            if deadline is not None and _round > 0 and _now() >= deadline:
                time_exhausted = True
                logger.info(
                    "Time budget of %.0fs spent after %d tool rounds, forcing text response (conversation=%s)",
                    time_budget,
                    _round,
                    conversation_id,
                )
                break

            tool_calls_accumulated: list[dict] = []
            text_buffer = ""

            # If budget is getting tight, omit tool definitions so the model
            # generates a text response instead of requesting more tool calls.
            send_tools = get_chat_tools(model=model) if usage_frac < _TOOL_LOOP_BUDGET else None

            try:
                payload: dict = {
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.3,
                }
                if send_tools:
                    payload["tools"] = send_tools

                llm_url = f"{settings.llama_server_url}/v1/chat/completions"

                # Retry once on 5xx — llama-server can transiently 500 on
                # MoE compute errors or KV cache issues.
                response_obj = None
                client_obj = None
                for _attempt in range(2):
                    client_obj = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
                    response_obj = await client_obj.send(
                        client_obj.build_request("POST", llm_url, json=payload),
                        stream=True,
                    )
                    if response_obj.status_code < 500 or _attempt == 1:
                        break
                    # 5xx on first attempt — close, wait, retry
                    await response_obj.aclose()
                    await client_obj.aclose()
                    report_llm_error(response_obj.status_code)
                    logger.warning("LLM returned %d, retrying in 2s", response_obj.status_code)
                    await asyncio.sleep(2)

                try:
                    response = response_obj

                    if response.status_code >= 400:
                        await response.aread()
                        detail = response.text[:2000]
                        if response.status_code >= 500:
                            report_llm_error(response.status_code)

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
                        early_cites = dedupe_citations(citations_accumulated)
                        done_payload: dict = {
                            "type": "done",
                            "context_pct": 100 if is_context_overflow else None,
                            "rag_context": {"citations": early_cites} if early_cites else None,
                        }
                        if conv and conv.title != "New conversation":
                            done_payload["title"] = conv.title
                        if active_model_id:
                            done_payload["model_id"] = active_model_id
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return

                    report_llm_success()
                    # Keepalives are keyed to the client's silence, not the model's (#731): a tool call or a
                    # model's thinking streams lines the client hears nothing of, for minutes at a time.
                    last_sent = _now()
                    # aclosing: leaving this loop with `break` must close the iterator now, not when the garbage
                    # collector gets to it, so its pending fetch is cancelled before the response is closed.
                    async with aclosing(_iter_with_keepalive(response.aiter_lines())) as lines:
                        async for line in lines:
                            if deadline is not None:
                                # One generation can outlast the whole budget (a 26B model at 11 tokens a
                                # second wrote for ten minutes at a stretch, #719). Closing the stream, in
                                # the finally below, is what stops llama-server decoding. A tool call being
                                # written is cut at the deadline; an answer already being written gets the
                                # same grace a forced answer would, and its "[DONE]" is let through: nothing
                                # was cut. A tool call's "[DONE]" is not: one that completes as the budget
                                # runs out is not searched either.
                                # Blank content is not an answer being written: a reasoning parser commonly
                                # streams "\n\n" as content once it has stripped a closing think tag.
                                writing_answer = bool(text_buffer.strip()) and not (
                                    tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]
                                )
                                answer_done = writing_answer and isinstance(line, str) and line[6:].strip() == "[DONE]"
                                cut_at = deadline + _FINAL_ANSWER_GRACE_SECONDS if writing_answer else deadline
                                if not answer_done and _now() >= cut_at:
                                    time_exhausted = True
                                    break
                            if line is _KEEPALIVE_SENTINEL:
                                yield ": keepalive\n\n"
                                last_sent = _now()
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
                                last_sent = _now()
                            elif _now() - last_sent >= _KEEPALIVE_INTERVAL:
                                yield ": keepalive\n\n"
                                last_sent = _now()
                finally:
                    await response_obj.aclose()
                    await client_obj.aclose()

            except httpx.TransportError:
                # Connect, timeout, or a reset mid-stream when the server is swapped or crashes while writing: to
                # the caller these are one event, the server is gone. Anything narrower let a ReadError out of
                # the generator, so the stream dropped with no error event and no stored message (#690).
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
                early_cites = dedupe_citations(citations_accumulated)
                done_payload: dict = {"type": "done"}
                if early_cites:
                    done_payload["rag_context"] = {"citations": early_cites}
                if conv and conv.title != "New conversation":
                    done_payload["title"] = conv.title
                if active_model_id:
                    done_payload["model_id"] = active_model_id
                yield f"data: {json.dumps(done_payload)}\n\n"
                return

            if time_exhausted:
                # Cut mid-generation. Text the user already watched arrive is the answer, or the start of it
                # when a tool call was being written beside it: that call is dropped and the rest of the
                # answer is forced from what the earlier rounds found, after the text the user saw.
                logger.info(
                    "Time budget of %.0fs spent during tool round %d, stopping the search (conversation=%s)",
                    time_budget,
                    _round + 1,
                    conversation_id,
                )
                if text_buffer.strip() and not (
                    tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]
                ):
                    assistant_content = text_buffer
                else:
                    streamed_prefix = text_buffer
                break

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

                    result_str = await execute_tool(fn_name, fn_args, user_id, user_scope=user_scope)

                    # Capture citations parsed from this tool result. Best-effort:
                    # tools that don't return citation-shaped JSON contribute nothing.
                    # Dedup happens later, after the whole tool loop, so the same
                    # chunk surfacing in two searches collapses to one cite.
                    citations_accumulated.extend(extract_citations_from_tool_result(result_str))

                    yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summarize_tool_result(result_str), 'raw_result': result_str})}\n\n"

                    # Save full result to DB, but truncate for LLM context
                    tool_msg = ChatMessage(
                        conversation_id=conversation_id,
                        role="tool",
                        content=result_str,
                        tool_call_id=tc.get("id", f"call_{fn_name}"),
                    )
                    session.add(tool_msg)
                    searched_this_turn = True
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
        stop_reason: str | None = None
        if time_exhausted:
            stop_reason = "time_budget"
        elif budget_exhausted:
            stop_reason = "context_budget"
        elif not assistant_content and _round == _MAX_TOOL_ROUNDS - 1:
            stop_reason = "tool_rounds"
        server_failure: str | None = None  # how the forced call itself failed, when it did
        thought_cut_short = False  # the forced answer is a thought cut short, under the app's lead
        produced_nothing = not assistant_content.strip()
        if produced_nothing and stop_reason:
            reason = {
                "time_budget": f"time budget of {time_budget:.0f}s",
                "context_budget": "context budget",
                "tool_rounds": f"{_MAX_TOOL_ROUNDS} tool rounds",
            }[stop_reason]
            logger.info("Forcing final text response after %s (conversation=%s)", reason, conversation_id)
            # The forced answer is bounded too, or the bound is not one. Both clocks start from the model's tokens,
            # not from the request: the prompt is re-read in full first (no tools this time, so the prompt cache
            # misses), and on a full context that alone can take most of two minutes, in silence. A keepalive
            # sentinel is our silence, not the model's token: it starts no clock, though a clock already running is
            # checked on it, so a model that falls silent past its grace is cut then. (The client's read timeout of
            # 120 s is a third bound on that silence, older than both.) The thinking bound runs from the model's
            # first token, the grace from its first content token (#742). A line carrying no token, the finish
            # chunk or "[DONE]", is let through whenever it arrives: it cuts nothing, and cutting it would turn a
            # finished answer into none.
            thinking_deadline: float | None = None
            final_deadline: float | None = None
            cut: str | None = None
            reasoning_buffer = ""  # the model's reasoning channel: the answer when no content comes (below)
            finish_reason: str | None = None  # "stop" when the model ended its answer, "length" when the server did
            # This turn's results, not the history's: an earlier turn's tool rows are in ``messages`` too, and
            # "the results above" would point the model at them.
            search_over = _SEARCH_OVER_MESSAGE if searched_this_turn else _SEARCH_OVER_UNSEARCHED_MESSAGE
            final_payload = {
                "messages": [*messages, {"role": "user", "content": search_over}],
                "stream": True,
                "temperature": 0.3,
                # No thinking before the answer: llama-server reads ``enable_thinking`` for the templates that take
                # it (Qwen3, Gemma 4) and passes ``reasoning_effort`` to gpt-oss's, which thinks regardless and
                # reasons least at "low". Not "none": at the pin (b29c606) that turns enable_thinking off for every
                # template and erases the reasoning_effort kwarg, so gpt-oss would get neither hint. Templates that
                # know neither ignore both.
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_effort": "low",
            }
            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client,
                    client.stream(
                        "POST",
                        f"{settings.llama_server_url}/v1/chat/completions",
                        json=final_payload,
                    ) as response,
                ):
                    if response.status_code >= 400:
                        await response.aread()
                        if response.status_code >= 500:
                            report_llm_error(response.status_code)
                        server_failure = f"HTTP {response.status_code}"
                        logger.warning(
                            "Forced final answer failed: %s %s (conversation=%s)",
                            server_failure,
                            response.text[:500],
                            conversation_id,
                        )
                    else:
                        last_sent = _now()
                        async with aclosing(_iter_with_keepalive(response.aiter_lines())) as lines:
                            async for line in lines:
                                if line is _KEEPALIVE_SENTINEL:
                                    if deadline is not None:
                                        cut = _forced_answer_cut(_now(), thinking_deadline, final_deadline)
                                        if cut:
                                            break
                                    yield ": keepalive\n\n"
                                    last_sent = _now()
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
                                choice = chunk.get("choices", [{}])[0]
                                finish_reason = choice.get("finish_reason") or finish_reason
                                delta = choice.get("delta", {})
                                token = delta.get("content") or ""
                                thought = delta.get("reasoning_content") or ""
                                if not token and not thought:
                                    continue
                                if deadline is not None:
                                    if thinking_deadline is None:
                                        thinking_deadline = _now() + _FINAL_ANSWER_THINKING_SECONDS
                                    cut = _forced_answer_cut(_now(), thinking_deadline, final_deadline)
                                    if cut:
                                        break
                                    if token and final_deadline is None:
                                        final_deadline = _now() + _FINAL_ANSWER_GRACE_SECONDS
                                if token:
                                    assistant_content += token
                                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                                    last_sent = _now()
                                else:
                                    reasoning_buffer += thought
                                    if _now() - last_sent >= _KEEPALIVE_INTERVAL:
                                        yield ": keepalive\n\n"
                                        last_sent = _now()
                        if cut == "grace":
                            logger.info(
                                "Final answer cut after %.0fs of grace (conversation=%s)",
                                _FINAL_ANSWER_GRACE_SECONDS,
                                conversation_id,
                            )
                        elif cut == "thinking":
                            logger.info(
                                "Final answer cut: no content %.0fs after the model's first token, %d chars of "
                                "reasoning (conversation=%s)",
                                _FINAL_ANSWER_THINKING_SECONDS,
                                len(reasoning_buffer),
                                conversation_id,
                            )
            except httpx.TransportError as exc:
                # A swap or a crash mid-answer resets the connection (#690): that is a lost server too, not a model
                # that wrote nothing.
                if isinstance(exc, httpx.TimeoutException):
                    server_failure = "timed out"
                elif isinstance(exc, httpx.ConnectError):
                    server_failure = "unreachable"
                else:
                    server_failure = "reset the connection"
                logger.warning(
                    "Forced final answer failed: the model server %s (%s) (conversation=%s)",
                    server_failure,
                    type(exc).__name__,
                    conversation_id,
                )
            if not assistant_content.strip() and reasoning_buffer.strip():
                # gpt-oss routes its whole answer through the reasoning channel even when asked not to think, and a
                # model that thinks regardless has only that channel to show for its time (research.py's streaming
                # path does the same). What it wrote there is shown: saying the model wrote nothing would be false.
                # An answer the model finished there is a plain answer. A thought cut short, by our bounds or by the
                # server's context, is shown under the app's lead, so it is not read (or judged) as an answer.
                # Streamed live it would have shown thinking as the answer while content might still follow, so it
                # arrives now, in one piece.
                logger.info(
                    "Final answer came as %d chars of reasoning_content and no content (finish_reason=%s, cut=%s) "
                    "(conversation=%s)",
                    len(reasoning_buffer),
                    finish_reason,
                    cut,
                    conversation_id,
                )
                if cut is not None:
                    assistant_content = _cut_thought_answer(reasoning_buffer, "time")
                    thought_cut_short = True
                elif finish_reason == "length":
                    assistant_content = _cut_thought_answer(reasoning_buffer, "context")
                    thought_cut_short = True
                else:
                    assistant_content = reasoning_buffer.strip()
                yield f"data: {json.dumps({'type': 'token', 'content': assistant_content})}\n\n"
            produced_nothing = not assistant_content.strip()
            if streamed_prefix:
                # Whatever the forced call produced, or did not: the stored message begins with what was watched.
                assistant_content = streamed_prefix + assistant_content

        # No answer where one was due: the app says so in its own words, after any text the user watched arrive.
        # The sentence is not the model's, so the note that the model "answered from what it had found" is not
        # appended to it, nor to a thought cut short, whose lead already says the time ran out.
        if produced_nothing:
            fallback = _nothing_produced_fallback(stop_reason, server_failure)
            if assistant_content.strip():
                fallback = "\n\n" + fallback
                assistant_content += fallback
            else:
                assistant_content = fallback
            yield f"data: {json.dumps({'type': 'token', 'content': fallback})}\n\n"
        if time_exhausted and not produced_nothing and not thought_cut_short:
            # The answer says it stopped early, in the text the user keeps (#719).
            note = _stopped_early_note(_now() - started_at)
            assistant_content += note
            yield f"data: {json.dumps({'type': 'token', 'content': note})}\n\n"

        # Estimate context usage for the UI indicator.
        # Include tool schema, all messages sent to the LLM, and the response.
        input_tokens = _estimate_messages_tokens(messages) + _get_tool_schema_tokens(model)
        response_tokens = _estimate_tokens(assistant_content) if assistant_content else 0
        used_tokens = input_tokens + response_tokens
        context_pct = round(min(used_tokens / context_tokens, 1.0) * 100) if context_tokens > 0 else 0

        # Save assistant response
        final_citations = dedupe_citations(citations_accumulated)
        rag_context_payload = {"citations": final_citations} if final_citations else None
        assistant_msg = None
        if assistant_content:
            assistant_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                tokens_used=total_tokens or None,
                rag_context=rag_context_payload,
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
            "rag_context": rag_context_payload,
        }
        if stop_reason:
            # Why the search stopped before the model chose to answer: "time_budget", "context_budget" or
            # "tool_rounds". Absent when the model answered on its own. The eval harness records a bounded
            # answer as degraded rather than as a plain completion.
            done_payload["stop_reason"] = stop_reason
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
