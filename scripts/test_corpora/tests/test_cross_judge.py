# scripts/test_corpora/tests/test_cross_judge.py
"""Unit tests for scripts/test_corpora/runner/cross_judge.py.

Uses a MockJudgeProvider to avoid live LLM calls; the live OpenAI
integration is exercised manually in the deferred live-smoke task.
"""

from __future__ import annotations

import json

from scripts.test_corpora.runner.cross_judge import JudgeProvider, rejudge_with


class _MockJudge:
    """Returns a canned verdict JSON string for every prompt. Used to drive
    rejudge_with without an OpenAI call.

    `responses` is a list of (correctness, groundedness, completeness, rationale)
    tuples consumed in order; raises if exhausted.
    """

    def __init__(self, responses: list[tuple] | None = None):
        self._responses = responses or [(4, 4, 4, "mock")]
        self._call_count = 0
        self.calls: list[str] = []

    def judge(self, prompt: str) -> str:
        self.calls.append(prompt)
        idx = min(self._call_count, len(self._responses) - 1)
        c, g, comp, rat = self._responses[idx]
        self._call_count += 1
        return json.dumps(
            {
                "correctness": c,
                "groundedness": g,
                "completeness": comp,
                "rationale": rat,
            }
        )


def _cap(qid: str, qtype: str = "lookup") -> dict:
    """Minimum capture shape rejudge_with needs."""
    return {
        "question_id": qid,
        "question": "q?",
        "answer": "an answer",
        "cited_doc_ids": ["abc"],
        "cited_doc_titles": ["A title"],
        "tool_call_count": 1,
        "tool_transcript": [{"tool": "kb_search", "args": {}, "result_summary": "{}"}],
        "elapsed_seconds": 0.0,
        "model": "baseline-model",
        "timestamp": "2026-05-25T00:00:00Z",
        "_qtype": qtype,  # rejudge_with reads this to pick the prompt template
    }


def test_mock_judge_satisfies_protocol():
    """The MockJudge structurally matches the Protocol — runtime check."""
    assert isinstance(_MockJudge(), JudgeProvider)


def test_rejudge_with_returns_verdict_per_capture():
    caps = [_cap("q1"), _cap("q2")]
    judge = _MockJudge(responses=[(5, 5, 5, "ok"), (3, 2, 4, "partial")])
    results = rejudge_with(caps, judge, judge_model="mock-model")
    assert len(results) == 2
    assert results[0]["qid"] == "q1"
    assert results[0]["correctness"] == 5
    assert results[0]["judge_model"] == "mock-model"
    assert results[1]["qid"] == "q2"
    assert results[1]["completeness"] == 4


def test_rejudge_with_items_cap_limits_sample_size():
    caps = [_cap(f"q{i}") for i in range(10)]
    judge = _MockJudge()
    results = rejudge_with(caps, judge, judge_model="mock-model", items=3)
    assert len(results) == 3


def test_rejudge_with_items_sample_is_deterministic():
    """Same captures + same seed should give the same selection."""
    caps = [_cap(f"q{i}") for i in range(10)]
    judge_a = _MockJudge()
    judge_b = _MockJudge()
    results_a = rejudge_with(caps, judge_a, judge_model="mock", items=3, seed=42)
    results_b = rejudge_with(caps, judge_b, judge_model="mock", items=3, seed=42)
    assert [r["qid"] for r in results_a] == [r["qid"] for r in results_b]


def test_rejudge_with_provider_error_records_judge_error():
    """A provider exception on one item must not abort the whole run."""

    class _RaisingJudge:
        def __init__(self):
            self.calls = 0

        def judge(self, prompt: str) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated rate limit")
            return json.dumps({"correctness": 5, "groundedness": 5, "completeness": 5, "rationale": "ok"})

    caps = [_cap(f"q{i}") for i in range(3)]
    results = rejudge_with(caps, _RaisingJudge(), judge_model="mock")
    assert len(results) == 3
    # Item 1 succeeded.
    assert "judge_error" not in results[0]
    # Item 2 errored.
    assert "judge_error" in results[1]
    assert "simulated rate limit" in results[1]["judge_error"]
    # Item 3 succeeded after the error (no early abort).
    assert "judge_error" not in results[2]


def test_rejudge_with_find_qtype_uses_find_prompt():
    """When the capture's qtype is 'find', the prompt must come from
    _PROMPT_FIND, not _PROMPT — verifiable by inspecting the prompt the
    MockJudge received."""
    caps = [_cap("q-find", qtype="find")]
    judge = _MockJudge()
    rejudge_with(caps, judge, judge_model="mock", answer_keys={"q-find": {"count": 5, "all": [], "sample": ["a", "b"]}})
    # _PROMPT_FIND has the marker "QUESTION TYPE: find"
    assert "QUESTION TYPE: find" in judge.calls[0]
