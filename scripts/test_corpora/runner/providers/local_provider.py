"""LocalProvider — answer-eval through HC's chat API.

The cloud providers (AnthropicProvider, OpenAIProvider) connect directly to
HC's MCP endpoint and drive the tool-use loop themselves. Local llama-server
models can't do that — they live INSIDE HC and are reached only via HC's
chat API. LocalProvider wraps that path so answer-eval mode can score them
the same way it scores cloud models: same BaselineResult shape, same judge,
same per-question report files.

Mechanics per run_question:
  1. Resolve the corpus's watch folder UUID once (cached).
  2. POST /chat/conversations with scope={folder_ids: [uuid]} so HC's chat
     engine restricts tool calls to that corpus's documents.
  3. POST /chat/conversations/{id}/messages and drain the SSE event stream.
  4. Collect tokens → answer; tool_call/tool_result events → tool_transcript;
     done event's rag_context.citations → cited_doc_ids/cited_doc_titles.
  5. Build BaselineResult identical in shape to the cloud captures.

Model activation is handled at construction time — one PUT
/chat/models/{id}/activate per LocalProvider instance. The activation
endpoint requires admin JWT, so this path needs HC_USERNAME/HC_PASSWORD
in the environment (the cloud answer-eval path can run with just
HC_API_KEY).
"""

from __future__ import annotations

import time
from typing import Any

from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.providers.base import BaselineResult


class LocalProvider:
    """Run a local HC-hosted model through HC's chat API and return the answer
    as a BaselineResult.

    Note: ``mcp_session`` is accepted for factory-signature compatibility but
    ignored — local models do tool calls through HC's chat engine, not direct
    MCP. Pass it as None or whatever the caller has.
    """

    def __init__(
        self,
        *,
        hc_client: HarborClerkClient,
        model: str,
        mcp_session: Any = None,
        doc_ids_seen: list[str] | None = None,
    ) -> None:
        self._hc = hc_client
        self._model = model
        # Maintained for factory parity with cloud providers — answer-eval's
        # per-question reset writes to this dict.
        self._cited: dict[str, str] = {}
        # Pre-seed per protocol; LocalProvider doesn't currently consult it.
        self._doc_ids_seen = list(doc_ids_seen or [])
        # Folder UUID cache so we don't re-fetch per question.
        self._folder_id_by_corpus: dict[str, str] = {}
        # Activate the model up-front so the first question doesn't pay the
        # warmup cost inside the SSE stream's timeout budget.
        self._hc.activate_model(model)
        self._hc.wait_for_model_ready(model, max_wait_seconds=600)

    def _folder_id_for(self, corpus: str) -> str:
        if corpus not in self._folder_id_by_corpus:
            self._folder_id_by_corpus[corpus] = self._hc.folder_id_for_corpus(corpus)
        return self._folder_id_by_corpus[corpus]

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        scope = {"folder_ids": [self._folder_id_for(corpus)]}
        title = f"answer-eval/{corpus}/{self._model}/{question_id}"
        conv_id = self._hc.create_conversation(title=title, scope=scope)

        start = time.time()
        events = list(self._hc.stream_ask(conv_id, question))
        elapsed = time.time() - start

        answer_chunks: list[str] = []
        tool_transcript: list[dict] = []
        cited_doc_ids: list[str] = []
        cited_doc_titles: list[str] = []
        tool_call_count = 0

        # Index tool_result events by tool_call_id so we can pair them up in
        # the transcript. HC's SSE emits tool_call (with id+args) and
        # tool_result (with id+result) as separate events.
        tool_results_by_id: dict[str, dict] = {}
        tool_calls_by_id: dict[str, dict] = {}

        for ev in events:
            etype = ev.get("type")
            if etype == "token":
                content = ev.get("content")
                if isinstance(content, str):
                    answer_chunks.append(content)
            elif etype == "tool_call":
                tool_call_count += 1
                tool_calls_by_id[ev.get("id") or f"_call_{tool_call_count}"] = ev
            elif etype == "tool_result":
                tool_results_by_id[ev.get("id") or f"_call_{tool_call_count}"] = ev
            elif etype == "done":
                rc = ev.get("rag_context") or {}
                citations = rc.get("citations") or ev.get("citations") or []
                for c in citations:
                    if isinstance(c, dict):
                        did = c.get("doc_id")
                        title_str = c.get("doc_title") or c.get("title") or ""
                        if did:
                            cited_doc_ids.append(str(did))
                            cited_doc_titles.append(str(title_str))
                            # Populate _cited so answer_eval's bookkeeping
                            # mirrors cloud providers' behavior.
                            self._cited[str(did)] = str(title_str)

        # Build a transcript in invocation order, pairing tool_call + tool_result.
        for call_id, call_ev in tool_calls_by_id.items():
            result_ev = tool_results_by_id.get(call_id, {})
            raw_result = result_ev.get("result")
            # Keep summary compact — full tool result blobs can be large.
            if isinstance(raw_result, str):
                summary = raw_result[:500]
            elif isinstance(raw_result, dict):
                summary = {k: v for k, v in raw_result.items() if k in ("hits", "passages", "count", "doc_id", "title")}
            else:
                summary = raw_result
            tool_transcript.append(
                {
                    "tool": call_ev.get("name") or call_ev.get("tool") or "",
                    "args": call_ev.get("arguments") or call_ev.get("args") or {},
                    "result_summary": summary,
                }
            )

        return BaselineResult(
            question_id=question_id,
            question=question,
            answer="".join(answer_chunks),
            cited_doc_ids=cited_doc_ids,
            cited_doc_titles=cited_doc_titles,
            tool_call_count=tool_call_count,
            tool_transcript=tool_transcript,
            elapsed_seconds=elapsed,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
        )
