"""Phase 1 — Claude baseline generator.

Runs Sonnet 4.6 with Harbor Clerk's MCP server attached as a tool source.
Captures the final answer plus the doc IDs surfaced via ``kb_search`` /
``kb_read_passages`` / ``kb_get_document`` tool calls. Saves one JSON
file per (corpus, question_id) under ``baselines/``.

The tool-call loop mirrors what an MCP client does: send the user's
question, get back tool_use blocks, execute them via the MCP session,
feed results back, repeat until ``stop_reason == 'end_turn'``.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import anthropic

SYSTEM_PROMPT = """You are answering a user's question about a specific document corpus.
You have access to the corpus only through the provided MCP tools (kb_search,
kb_read_passages, kb_get_document, kb_find_related, etc.). Use them as
needed. Cite specific documents in your answer by doc_id.

Be thorough — your answer is the gold-standard reference for evaluating
local models. Spend tool calls liberally."""


@dataclasses.dataclass
class BaselineResult:
    question_id: str
    question: str
    answer: str
    cited_doc_ids: list[str]
    # Stable, ingest-independent identifiers for the cited docs, parallel to
    # cited_doc_ids (same order). doc_id is a random per-ingest UUID, so a
    # baseline's cited_doc_ids never intersect a phase-4 response's doc_ids
    # unless both ran against the exact same ingest. doc_title (the filename
    # stem) is stable across re-ingests, so citation_overlap computed on
    # titles stays correct even when baseline and response come from
    # different ingests. Empty string for any cited doc whose tool result
    # carried no title.
    cited_doc_titles: list[str]
    tool_call_count: int
    # Per-call record [{tool, args, result_summary}] in call order — for
    # groundedness scoring and tool-use auditing.
    tool_transcript: list[dict]
    elapsed_seconds: float
    model: str
    timestamp: str


class BaselineGenerator:
    """Runs Sonnet 4.6 + MCP for one question.

    The constructor takes an opaque ``mcp_session`` — in production this is
    an ``mcp.ClientSession`` connected to Harbor Clerk's MCP server. In tests,
    it can be ``None`` if the mock anthropic client doesn't actually emit
    tool_use blocks.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        mcp_session: Any,
        model: str = "claude-sonnet-4-6",
        doc_ids_seen: list[str] | None = None,
    ):
        self._client = client
        self._mcp = mcp_session
        self._model = model
        # Ordered map doc_id -> doc_title, in first-seen order. dict preserves
        # insertion order (3.7+), so cited_doc_ids and cited_doc_titles stay
        # parallel. For tests: pre-seed via doc_ids_seen (titles default to "").
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

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
                system=SYSTEM_PROMPT,
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
        out = results_dir / "baselines" / corpus / f"{result.question_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dataclasses.asdict(result), indent=2))
        return out
