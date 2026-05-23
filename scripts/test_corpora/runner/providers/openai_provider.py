# scripts/test_corpora/runner/providers/openai_provider.py
"""OpenAIProvider — gpt-* (and o1-*/o3-*) + Harbor Clerk MCP.

Structurally mirrors AnthropicProvider but speaks OpenAI's chat.completions
tool_calls protocol:
  1. Send user message + tools list.
  2. Receive a response with `finish_reason`:
     - "stop"        -> done, return final assistant text
     - "tool_calls"  -> execute each tool, send results back, loop
     - "length"      -> response truncated; treat as end-of-turn (logged warn)
  3. Tool result messages have role="tool", tool_call_id=<call id>.

Constructor accepts client=None for symmetry with AnthropicProvider; when
None, openai.OpenAI() reads OPENAI_API_KEY from env.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import openai

from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
)

log = logging.getLogger("openai_provider")


class OpenAIProvider:
    """Runs a gpt-* (or o1-*/o3-*) model + Harbor Clerk MCP for one question."""

    def __init__(
        self,
        *,
        mcp_session: Any,
        model: str = "gpt-4o",
        client: openai.OpenAI | None = None,
        doc_ids_seen: list[str] | None = None,
    ):
        # Lazy client construction — openai.OpenAI() validates OPENAI_API_KEY at
        # __init__ time (anthropic.Anthropic() doesn't), so eager construction
        # in the factory's no-API-call path (e.g. factory dispatch tests)
        # would force every caller to have OPENAI_API_KEY set. Defer to first
        # API call instead.
        self._explicit_client = client
        self._mcp = mcp_session
        self._model = model
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

    @property
    def _client(self) -> openai.OpenAI:
        if self._explicit_client is None:
            self._explicit_client = openai.OpenAI()
        return self._explicit_client

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to OpenAI's `tools` schema.

        OpenAI shape: [{"type": "function", "function": {"name", "description",
        "parameters"}}]. The MCP tool's `inputSchema` slots directly into
        `function.parameters` (both are JSON Schema).
        """
        if self._mcp is None:
            return []
        tools = self._mcp.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in tools
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

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        started = time.time()
        tools = self._list_tools()
        messages: list[dict] = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        tool_call_count = 0
        tool_transcript: list[dict] = []
        final_text = ""

        while True:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": 8000,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            resp = self._client.chat.completions.create(**kwargs)
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

            if finish == "tool_calls" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_count += 1
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    out = self._exec_tool(tc.function.name, args)
                    tool_transcript.append(
                        {
                            "tool": tc.function.name,
                            "args": args,
                            "result_summary": out[:600],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": out,
                        }
                    )
                continue  # loop to next assistant turn

            # finish in {"stop", "length", "content_filter", "function_call", ...}
            if finish == "length":
                log.warning("openai response truncated (finish_reason=length) — returning partial answer")
            final_text = msg.content or ""
            break

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
        )
