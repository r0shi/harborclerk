# scripts/test_corpora/tests/test_local_provider.py
"""LocalProvider — exercises the SSE-event → BaselineResult shape, with
the HC chat event names (`summary` / `raw_result`) rather than the older
`result` key.

The full provider talks to HC's chat API for model activation and
conversation creation; these tests stub the HarborClerkClient surface
just enough to drive `run_question`.
"""

from __future__ import annotations

from typing import Any

from scripts.test_corpora.runner.providers.local_provider import LocalProvider


class _StubHC:
    """Minimal HarborClerkClient stub. Records calls; replays events."""

    def __init__(self, events: list[dict[str, Any]], folder_id: str = "FOLDER-1") -> None:
        self._events = events
        self._folder_id = folder_id
        self.created_conversations: list[dict] = []
        self.streamed_questions: list[tuple[str, str]] = []

    def activate_model(self, model: str) -> None:
        return None

    def wait_for_model_ready(self, model: str, max_wait_seconds: int = 600) -> None:
        return None

    def folder_id_for_corpus(self, corpus: str) -> str:
        return self._folder_id

    def create_conversation(self, *, title: str, scope: dict | None = None) -> str:
        self.created_conversations.append({"title": title, "scope": scope})
        return "CONV-1"

    def stream_ask(self, conv_id: str, question: str):
        self.streamed_questions.append((conv_id, question))
        yield from self._events


def _events_with_tool_summary(summary: str = '{"hits":[{"doc_id":"D1"}]}'):
    return [
        {"type": "tool_call", "name": "search_documents", "arguments": {"query": "x"}},
        {"type": "tool_result", "name": "search_documents", "summary": summary, "raw_result": "FULL-" + summary},
        {"type": "token", "content": "Answer "},
        {"type": "token", "content": "text."},
        {
            "type": "done",
            "rag_context": {
                "citations": [
                    {"doc_id": "D1", "doc_title": "Doc One"},
                ]
            },
        },
    ]


def test_tool_result_summary_uses_summary_field():
    """Regression: LocalProvider previously read `result_ev.get('result')`,
    which always returned None because HC's SSE uses `summary` / `raw_result`.
    The transcript's `result_summary` must contain the actual tool output so
    the answer-judge can verify groundedness against retrieved evidence.
    """
    hc = _StubHC(_events_with_tool_summary())
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    result = provider.run_question("q?", "qid-1", "cuad")

    assert len(result.tool_transcript) == 1
    entry = result.tool_transcript[0]
    assert entry["tool"] == "search_documents"
    assert entry["args"] == {"query": "x"}
    assert entry["result_summary"] == '{"hits":[{"doc_id":"D1"}]}'


def test_tool_result_summary_falls_back_to_raw_result():
    """If HC ever emits only `raw_result` without `summary`, the transcript
    should still capture the tool output rather than dropping it.
    """
    events = [
        {"type": "tool_call", "name": "read_passages", "arguments": {"chunk_ids": ["c1"]}},
        {"type": "tool_result", "name": "read_passages", "raw_result": "PASSAGE TEXT"},
        {"type": "done", "rag_context": {"citations": []}},
    ]
    hc = _StubHC(events)
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    result = provider.run_question("q?", "qid-2", "cuad")

    assert result.tool_transcript[0]["result_summary"] == "PASSAGE TEXT"


def test_tool_result_summary_truncates_long_strings():
    """Captures get checked into JSON and shipped to the judge; an unbounded
    raw_result would balloon both. Truncate at 500 chars to match the prior
    cap.
    """
    long_summary = "x" * 5000
    events = [
        {"type": "tool_call", "name": "search_documents", "arguments": {}},
        {"type": "tool_result", "name": "search_documents", "summary": long_summary},
        {"type": "done", "rag_context": {"citations": []}},
    ]
    hc = _StubHC(events)
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    result = provider.run_question("q?", "qid-3", "cuad")

    assert len(result.tool_transcript[0]["result_summary"]) == 500


def test_tool_call_without_matching_result_still_appears_in_transcript():
    """If a tool_call is emitted but the corresponding tool_result is dropped
    (e.g., the stream ends mid-turn), the call still belongs in the
    transcript — the model did invoke it. result_summary should be None.
    """
    events = [
        {"type": "tool_call", "name": "search_documents", "arguments": {"query": "y"}},
        {"type": "done", "rag_context": {"citations": []}},
    ]
    hc = _StubHC(events)
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    result = provider.run_question("q?", "qid-4", "cuad")

    assert len(result.tool_transcript) == 1
    assert result.tool_transcript[0]["tool"] == "search_documents"
    assert result.tool_transcript[0]["args"] == {"query": "y"}
    assert result.tool_transcript[0]["result_summary"] is None


def test_citations_populate_cited_ids_titles_in_order():
    """HC chat emits done.rag_context.citations as the accumulated set; the
    provider should preserve order and populate both ids and titles.
    """
    events = [
        {
            "type": "done",
            "rag_context": {
                "citations": [
                    {"doc_id": "D1", "doc_title": "T1"},
                    {"doc_id": "D2", "doc_title": "T2"},
                ]
            },
        }
    ]
    hc = _StubHC(events)
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    result = provider.run_question("q?", "qid-5", "cuad")

    assert result.cited_doc_ids == ["D1", "D2"]
    assert result.cited_doc_titles == ["T1", "T2"]


def test_scope_is_set_to_folder_for_corpus():
    """run_question should pass the corpus's watched-folder UUID to the
    conversation's scope so HC restricts tool calls to that corpus.
    """
    events = [{"type": "done", "rag_context": {"citations": []}}]
    hc = _StubHC(events, folder_id="FOLDER-CUAD-UUID")
    provider = LocalProvider(hc_client=hc, model="qwen3-4b")

    provider.run_question("q?", "qid-6", "cuad")

    assert hc.created_conversations[0]["scope"] == {"folder_ids": ["FOLDER-CUAD-UUID"]}
