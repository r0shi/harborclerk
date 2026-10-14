"""OpenAIProvider — gpt-* (and o1-*/o3-*) + Harbor Clerk MCP.

Structurally mirrors AnthropicProvider: one bounded tool loop (MAX_MODEL_CALLS, the last call with tools off), two
wire protocols, chosen by model:

  chat.completions, for gpt-4o and the gpt-5.6 family. Tools as {"type": "function", "function": {...}}; the
  model answers with `tool_calls`, each answered by a role="tool" message. `finish_reason`: "stop" is the end of
  the turn, "tool_calls" loops, "length" is a truncation (returned as the partial answer, said so in stopped_by).

  responses, for GPT-6 and later (gpt-6-astra, gpt-6.1-sol, gpt-6-sol). OpenAI's function-calling guide and the
  model pages (read 2026-10-14): GPT-6 Astra and GPT-6.1 Sol "require the Responses API for tool calling"; Chat
  Completions "is supported without tool calling". Tools are flat ({"type": "function", "name", "description",
  "parameters"}), the system prompt is `instructions`, the output limit is `max_output_tokens` (reasoning tokens
  count against it), the model answers with `function_call` output items (call_id, name, arguments) answered by
  `function_call_output` input items, and the model's whole output goes back with them: "any reasoning items
  returned in model responses with tool calls must also be passed back with tool call outputs". Reasoning effort
  is left at the model's default (the guide states medium for GPT-6.1 Sol and does not state GPT-6 Astra's);
  `temperature` is not sent (the reasoning models refuse it). Reasoning tokens are billed as output and reported
  in `usage.output_tokens_details.reasoning_tokens`, which the meter keeps apart. The
  cached prefix is in `usage.input_tokens_details.cached_tokens` (`prompt_tokens_details` on chat), which the
  meter prices at the cached rate.

Constructor accepts client=None for symmetry with AnthropicProvider; when
None, the metered client from runner/spend.py is built on first use.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import openai

from scripts.test_corpora.runner import spend
from scripts.test_corpora.runner.providers.base import (
    ANSWER_NOW,
    DEFAULT_SYSTEM_PROMPT,
    MAX_MODEL_CALLS,
    BaselineResult,
    checked_spend_kind,
)

log = logging.getLogger("openai_provider")

# Models that take their tools through the Responses API (see the module docstring). A prefix match, so that
# gpt-6-sol and the next 6.x land on the right protocol without an edit here.
RESPONSES_API_MODELS = ("gpt-6",)
# Output limit per call. The Responses limit covers a reasoning model's reasoning as well as its answer, so it
# gets twice the room; the meter reserves either at the output price before the call.
MAX_COMPLETION_TOKENS = 8000
MAX_OUTPUT_TOKENS = 16000


def uses_responses_api(model: str) -> bool:
    return model.startswith(RESPONSES_API_MODELS)


# Retry budget for openai.RateLimitError (429) inside run_question. The SDK
# does its own short retries (~5s) but a TPM-window reset takes up to 60s,
# so the SDK gives up before the window clears on low-tier keys. We retry
# with explicit exponential backoff (30s, 60s, 120s, 240s — capped) and
# give up after the budget is exhausted, in which case the error propagates
# and the eval runner can leave the unit PENDING for --rejudge later.
_OPENAI_RATE_LIMIT_RETRY_DELAYS = (30.0, 60.0, 120.0, 240.0)


def _as_item(output_item: Any) -> Any:
    """A Responses output item as it goes back in `input`: the SDK's pydantic objects dump to the dicts the API
    takes; a dict goes as it is."""
    if isinstance(output_item, dict):
        return output_item
    dump = getattr(output_item, "model_dump", None)
    if callable(dump):
        # By alias: the SDK spells a field the API calls `async` as `async_`, and a dump by field name would send
        # an unknown parameter back on the next call.
        return dump(exclude_none=True, by_alias=True)
    return {k: v for k, v in vars(output_item).items() if v is not None}


def _output_text(output: list[Any]) -> tuple[str, bool]:
    """(the text of the last message item, whether any message part was a refusal)."""
    text, refused = "", False
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        parts = []
        for part in getattr(item, "content", None) or []:
            kind = getattr(part, "type", None)
            if kind == "output_text":
                parts.append(getattr(part, "text", "") or "")
            elif kind == "refusal":
                refused = True
        if parts:
            text = "".join(parts)
    return text, refused


class OpenAIProvider:
    """Runs a gpt-* (or o1-*/o3-*) model + Harbor Clerk MCP for one question."""

    def __init__(
        self,
        *,
        mcp_session: Any,
        model: str = "gpt-4o",
        client: openai.OpenAI | None = None,
        doc_ids_seen: list[str] | None = None,
        spend_kind: str = "baseline_question",
    ):
        # Lazy client construction — openai.OpenAI() validates OPENAI_API_KEY at
        # __init__ time (anthropic.Anthropic() doesn't), so eager construction
        # in the factory's no-API-call path (e.g. factory dispatch tests)
        # would force every caller to have OPENAI_API_KEY set. Defer to first
        # API call instead.
        self._explicit_client = client
        self._mcp = mcp_session
        self._model = model
        # What this provider's calls and units are booked as. A sweep baseline is a `baseline_question`;
        # answer-eval and rerun_pr_j answer other questions and must not be priced as one (spend.yaml).
        self._spend_kind = checked_spend_kind(spend_kind)
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

    @property
    def _client(self) -> openai.OpenAI:
        if self._explicit_client is None:
            self._explicit_client = spend.openai_client(self._spend_kind)
        return self._explicit_client

    def _mcp_tools(self) -> list[Any]:
        return [] if self._mcp is None else list(self._mcp.list_tools())

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to chat.completions' `tools` schema.

        OpenAI shape: [{"type": "function", "function": {"name", "description",
        "parameters"}}]. The MCP tool's `inputSchema` slots directly into
        `function.parameters` (both are JSON Schema).
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in self._mcp_tools()
        ]

    def _list_tools_flat(self) -> list[dict]:
        """The same tools in the Responses API's flat shape."""
        return [
            {"type": "function", "name": t.name, "description": t.description or "", "parameters": t.inputSchema}
            for t in self._mcp_tools()
        ]

    def _exec_tool(self, name: str, args: dict) -> str:
        """Execute one MCP tool call, capture any doc_ids in the result."""
        result = self._mcp.call_tool(name, args)
        text_chunks: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                text_chunks.append(block.text)
                try:
                    parsed = json.loads(block.text)
                    self._collect_doc_ids(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        return "\n".join(text_chunks) or "(empty)"

    def _collect_doc_ids(self, obj: Any) -> None:
        if isinstance(obj, dict):
            doc_id = obj.get("doc_id")
            if isinstance(doc_id, str):
                title = obj.get("doc_title") or obj.get("title") or ""
                if doc_id not in self._cited or (title and not self._cited[doc_id]):
                    self._cited[doc_id] = title
            for v in obj.values():
                self._collect_doc_ids(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect_doc_ids(v)

    def _create_with_rate_limit_retry(self, send):
        """Call `send()` (one create on the metered client), retrying openai.RateLimitError (429) with exponential
        backoff.

        The OpenAI SDK's built-in retry obeys the API's Retry-After hint
        (often "28ms" for TPM limits even though the window is actually 60s).
        The SDK exhausts its short retry budget and propagates the 429
        before the TPM window clears. We absorb that by retrying with a
        30s/60s/120s/240s schedule on top of the metered client's own retries (runner/spend.py; the SDK is built with max_retries=0).

        Looks up ``time.sleep`` at call time (not via default-arg binding)
        so tests can patch ``time.sleep`` without monkey-patching the
        provider's __init__ signature.
        """
        for attempt, delay in enumerate(_OPENAI_RATE_LIMIT_RETRY_DELAYS, start=1):
            try:
                return send()
            except openai.RateLimitError:
                log.warning(
                    "openai 429 (rate limit) — backing off %.0fs before retry (attempt %d/%d)",
                    delay,
                    attempt,
                    len(_OPENAI_RATE_LIMIT_RETRY_DELAYS),
                )
                time.sleep(delay)
        # Final attempt — let any further error propagate
        return send()

    def _record_tool_call(self, name: str, raw_args: str | None, transcript: list[dict]) -> str:
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        out = self._exec_tool(name, args)
        transcript.append({"tool": name, "args": args, "result_summary": out[:600]})
        return out

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        started = time.time()
        loop = self._run_responses if uses_responses_api(self._model) else self._run_chat
        final_text, tool_call_count, model_call_count, tool_transcript, stopped_by = loop(question, question_id, corpus)
        spend.get_meter().count_unit(self._spend_kind)
        return BaselineResult(
            question_id=question_id,
            question=question,
            answer=final_text,
            cited_doc_ids=list(self._cited.keys()),
            cited_doc_titles=list(self._cited.values()),
            tool_call_count=tool_call_count,
            tool_transcript=tool_transcript,
            elapsed_seconds=time.time() - started,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            stopped_by=stopped_by,
            model_call_count=model_call_count,
        )

    def _run_chat(self, question: str, question_id: str, corpus: str) -> tuple[str, int, int, list[dict], str]:
        tools = self._list_tools()
        messages: list[dict] = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        tool_call_count = model_call_count = 0
        tool_transcript: list[dict] = []
        final_text = ""
        stopped_by = "unrecorded"

        # Bounded, like the Anthropic loop (MAX_MODEL_CALLS): the last call is made with tools off. OpenAI caches
        # a repeated prefix on its own, so there are no breakpoints to place here.
        for call in range(1, MAX_MODEL_CALLS + 1):
            last = call == MAX_MODEL_CALLS
            if last:
                messages.append({"role": "user", "content": ANSWER_NOW})
            kwargs: dict[str, Any] = {
                "model": self._model,
                # max_completion_tokens replaced max_tokens in OpenAI's API
                # starting with the o1 / gpt-5 families. It's accepted by
                # older models too (gpt-4o, gpt-4-turbo) for back-compat, so
                # we use the new name universally.
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "none" if last else "auto"
            resp = self._create_with_rate_limit_retry(lambda kw=kwargs: self._client.chat.completions.create(**kw))
            model_call_count += 1
            choice = resp.choices[0]
            msg = choice.message
            finish = choice.finish_reason

            # Persist the assistant message before loop-deciding so subsequent
            # tool results pair with it correctly. OpenAI requires the
            # assistant message that contained the tool_calls to be in the
            # history before the tool-result messages.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            if last:
                stopped_by = "model_call_limit"
                log.warning("baseline %s/%s stopped at %d model calls", corpus, question_id, MAX_MODEL_CALLS)
            if finish == "tool_calls" and msg.tool_calls and not last:
                for tc in msg.tool_calls:
                    tool_call_count += 1
                    out = self._record_tool_call(tc.function.name, tc.function.arguments, tool_transcript)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue  # loop to next assistant turn

            # finish in {"stop", "length", "content_filter", "function_call", ...}
            if finish == "length":
                log.warning("openai response truncated (finish_reason=length) — returning partial answer")
            final_text = msg.content or ""
            if not last:
                # "stop" is OpenAI's end of turn; "length" and "content_filter" are not the model finishing.
                stopped_by = "end_turn" if finish == "stop" else str(finish)
            break
        return final_text, tool_call_count, model_call_count, tool_transcript, stopped_by

    def _run_responses(self, question: str, question_id: str, corpus: str) -> tuple[str, int, int, list[dict], str]:
        tools = self._list_tools_flat()
        items: list[Any] = [{"role": "user", "content": question}]
        tool_call_count = model_call_count = 0
        tool_transcript: list[dict] = []
        final_text = ""
        stopped_by = "unrecorded"

        for call in range(1, MAX_MODEL_CALLS + 1):
            last = call == MAX_MODEL_CALLS
            if last:
                items.append({"role": "user", "content": ANSWER_NOW})
            kwargs: dict[str, Any] = {
                "model": self._model,
                "instructions": DEFAULT_SYSTEM_PROMPT,
                "input": list(items),
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "none" if last else "auto"
            resp = self._create_with_rate_limit_retry(lambda kw=kwargs: self._client.responses.create(**kw))
            model_call_count += 1
            output = list(getattr(resp, "output", None) or [])
            # The whole output goes back, reasoning items included, before the results of the calls it made.
            items.extend(_as_item(item) for item in output)
            calls = [item for item in output if getattr(item, "type", None) == "function_call"]

            if last:
                stopped_by = "model_call_limit"
                log.warning("baseline %s/%s stopped at %d model calls", corpus, question_id, MAX_MODEL_CALLS)
            if calls and not last:
                for fc in calls:
                    tool_call_count += 1
                    out = self._record_tool_call(fc.name, fc.arguments, tool_transcript)
                    items.append({"type": "function_call_output", "call_id": fc.call_id, "output": out})
                continue

            final_text, refused = _output_text(output)
            status = getattr(resp, "status", None)
            if status == "incomplete":
                reason = getattr(getattr(resp, "incomplete_details", None), "reason", None) or "incomplete"
                log.warning("openai response incomplete (%s) — returning partial answer", reason)
            if not last:
                # "completed" is the end of the turn; a refusal part, or an incomplete response (max_output_tokens,
                # content_filter), is not the model finishing.
                if refused:
                    stopped_by = "refusal"
                elif status == "completed":
                    stopped_by = "end_turn"
                elif status == "incomplete":
                    stopped_by = str(reason)
                else:
                    stopped_by = str(status)
            break
        return final_text, tool_call_count, model_call_count, tool_transcript, stopped_by
