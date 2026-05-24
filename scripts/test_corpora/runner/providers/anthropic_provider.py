# scripts/test_corpora/runner/providers/anthropic_provider.py
"""AnthropicProvider — Sonnet (or any claude-* model) + Harbor Clerk MCP.

Body is the pre-refactor BaselineGenerator.run_question loop, lifted out of
claude_baseline.py verbatim. claude_baseline.py becomes a thin re-export
shim in Task 3 so sweep.py's existing import sites stay untouched.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import anthropic

from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
)


class AnthropicProvider:
    """Runs a Sonnet (or other claude-*) model + Harbor Clerk MCP for one question.

    Constructor accepts `client=None` for back-compat with the pre-refactor
    `BaselineGenerator(client=..., mcp_session=...)` call signature in
    sweep.py — when not provided, the factory's convenience role lazily
    constructs `anthropic.Anthropic()` (which reads ANTHROPIC_API_KEY from
    env). Tests pass a mock client explicitly.
    """

    def __init__(
        self,
        *,
        mcp_session: Any,
        model: str = "claude-sonnet-4-6",
        client: anthropic.Anthropic | None = None,
        doc_ids_seen: list[str] | None = None,
    ):
        # Lazy client construction (symmetric with OpenAIProvider). Defer
        # anthropic.Anthropic() until first API call so factory dispatch
        # tests don't need ANTHROPIC_API_KEY set.
        self._explicit_client = client
        self._mcp = mcp_session
        self._model = model
        # Ordered map doc_id -> doc_title, in first-seen order. dict preserves
        # insertion order (3.7+), so cited_doc_ids and cited_doc_titles stay
        # parallel. For tests: pre-seed via doc_ids_seen (titles default to "").
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

    @property
    def _client(self) -> anthropic.Anthropic:
        if self._explicit_client is None:
            self._explicit_client = anthropic.Anthropic()
        return self._explicit_client

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to Anthropic's tool schema."""
        if self._mcp is None:
            return []
        # mcp.types.Tool has .name, .description, .inputSchema
        tools = self._mcp.list_tools()  # sync wrapper expected
        return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in tools]

    def _exec_tool(self, name: str, args: dict) -> str:
        """Execute one MCP tool call, capture any doc_ids in the result."""
        result = self._mcp.call_tool(name, args)
        # Collect text content; capture doc_ids if present in the JSON
        text_chunks: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                text_chunks.append(block.text)
                # Greedy extraction: if the tool returned JSON containing
                # doc_id fields, harvest them. Robust to nested structures.
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
                # Pair the doc_id with its title from the SAME dict. MCP tool
                # results (kb_search etc.) emit doc_id + doc_title together;
                # fall back to "title" then "". First-seen title wins — if a
                # later result re-mentions the doc with a title where the
                # first had none, upgrade the stored value.
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
        messages: list[dict] = [{"role": "user", "content": question}]
        tool_call_count = 0
        tool_transcript: list[dict] = []
        resp = None

        while True:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=8000,
                system=DEFAULT_SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                break
            if resp.stop_reason != "tool_use":
                break  # safety

            # Execute each tool_use block, send results back as user message
            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_call_count += 1
                    out = self._exec_tool(block.name, block.input)
                    tool_transcript.append(
                        {
                            "tool": block.name,
                            "args": dict(block.input),
                            "result_summary": out[:600],
                        }
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        # Final answer is the last text block in the assistant turn — scan from
        # the end so a leading non-text block (e.g. tool_use) is skipped.
        final = ""
        if resp and resp.content:
            for block in reversed(resp.content):
                if hasattr(block, "text"):
                    final = block.text
                    break

        return BaselineResult(
            question_id=question_id,
            question=question,
            answer=final,
            cited_doc_ids=list(self._cited.keys()),
            cited_doc_titles=list(self._cited.values()),
            tool_call_count=tool_call_count,
            tool_transcript=tool_transcript,
            elapsed_seconds=time.time() - started,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @staticmethod
    def write(result: BaselineResult, results_dir: Path, corpus: str) -> Path:
        """Write a BaselineResult JSON under results_dir/baselines/<corpus>/.

        Kept as a @staticmethod (not a free function) for shim compatibility:
        the sweep.py phase-1 baseline calls `BaselineGenerator.write(...)`,
        which the shim re-exports as `AnthropicProvider.write`.
        """
        out = results_dir / "baselines" / corpus / f"{result.question_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dataclasses.asdict(result), indent=2))
        return out
