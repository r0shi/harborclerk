# scripts/test_corpora/tests/test_cross_judge.py
"""Unit tests for scripts/test_corpora/runner/cross_judge.py.

Uses a MockJudgeProvider to avoid live LLM calls; the live OpenAI
integration is exercised manually in the deferred live-smoke task.
"""

from __future__ import annotations

import json

import pytest

from scripts.test_corpora.runner.cross_judge import (
    JudgeProvider,
    _cohens_kappa,
    _spearman,
    compare_judges,
    rejudge_with,
)


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


# ---------------------------------------------------------------------------
# Task 5 — compare_judges + hand-rolled stats
# ---------------------------------------------------------------------------


def _ver(qid: str, c: int, g: int, comp: int, rat: str = "") -> dict:
    return {
        "qid": qid,
        "correctness": c,
        "groundedness": g,
        "completeness": comp,
        "rationale": rat,
    }


def test_spearman_identical_arrays_is_one():
    assert _spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_spearman_reversed_arrays_is_minus_one():
    assert _spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_with_ties_uses_rank_average():
    """Hand-computed: [1, 2, 2, 3] vs [10, 20, 20, 30] -> ranks identical -> 1.0"""
    assert _spearman([1, 2, 2, 3], [10, 20, 20, 30]) == pytest.approx(1.0)


def test_cohens_kappa_perfect_agreement_is_one():
    assert _cohens_kappa([0, 1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_cohens_kappa_random_assignment_near_zero():
    """All A=0, B distributed across [0..5]. Observed agreement equals
    chance agreement, so kappa ~= 0."""
    a = [0] * 6
    b = [0, 1, 2, 3, 4, 5]
    # observed agreement = 1/6; chance agreement also ~1/6 (B is uniform).
    # kappa ~= 0 (allow tolerance).
    assert abs(_cohens_kappa(a, b)) < 0.3


def test_compare_judges_empty_inputs_shape():
    result = compare_judges([], [])
    assert result == {
        "n": 0,
        "judges": [None, None],
        "deltas": {},
        "spearman": {},
        "kappa": {},
        "disagreements": [],
    }


def test_compare_judges_identical_verdicts_zero_delta():
    a = [_ver("q1", 5, 5, 5), _ver("q2", 3, 4, 5)]
    b = [_ver("q1", 5, 5, 5), _ver("q2", 3, 4, 5)]
    result = compare_judges(a, b, judges=("sonnet", "gpt-4o"))
    assert result["n"] == 2
    assert result["judges"] == ["sonnet", "gpt-4o"]
    for dim in ("correctness", "groundedness", "completeness"):
        assert result["deltas"][dim]["mean"] == 0.0
        assert result["spearman"][dim] == pytest.approx(1.0)
        assert result["kappa"][dim] == pytest.approx(1.0)
    assert result["disagreements"] == []


def test_compare_judges_disagreement_surfaces_on_delta_two():
    a = [_ver("q1", 4, 5, 4, rat="a-rat"), _ver("q2", 3, 3, 3)]
    b = [_ver("q1", 2, 5, 4, rat="b-rat"), _ver("q2", 3, 3, 3)]
    result = compare_judges(a, b, judges=("sonnet", "gpt-4o"))
    assert len(result["disagreements"]) == 1
    dis = result["disagreements"][0]
    assert dis["qid"] == "q1"
    assert dis["delta_dim"] == "correctness"
    assert dis["max_delta"] == 2
    assert dis["judge_a"]["correctness"] == 4
    assert dis["judge_b"]["correctness"] == 2
    assert dis["judge_a"]["rationale"] == "a-rat"
    assert dis["judge_b"]["rationale"] == "b-rat"


def test_compare_judges_mismatched_qids_intersection_only():
    """If one judge has verdicts the other doesn't, only the intersection counts."""
    a = [_ver("q1", 5, 5, 5), _ver("q2", 3, 3, 3)]
    b = [_ver("q1", 5, 5, 5), _ver("q3", 4, 4, 4)]
    result = compare_judges(a, b)
    assert result["n"] == 1  # only q1 intersected


def test_compare_judges_skips_judge_error_items():
    """Items with judge_error in either judge are skipped."""
    a = [_ver("q1", 5, 5, 5), _ver("q2", 4, 4, 4)]
    b = [_ver("q1", 5, 5, 5), {"qid": "q2", "judge_error": "rate limit"}]
    result = compare_judges(a, b)
    assert result["n"] == 1


# ── PR-J: _build_prompt delegates to render_prompt ────────────────────


def test_build_prompt_delegates_to_render_prompt():
    """A capture-shaped dict + qtype + answer_key passed through _build_prompt
    must produce byte-identical output to a direct render_prompt() call."""
    from scripts.test_corpora.runner.answer_judge import render_prompt
    from scripts.test_corpora.runner.cross_judge import _build_prompt

    cap = {
        "question": "What's the governing law?",
        "answer": "Delaware",
        "cited_doc_titles": ["contract.pdf", "amendment.pdf"],
    }
    via_wrapper = _build_prompt(cap, qtype="lookup", answer_key="Delaware")
    via_render = render_prompt(
        question="What's the governing law?",
        model_answer="Delaware",
        cited="- contract.pdf\n- amendment.pdf",
        answer_key="Delaware",
        qtype="lookup",
    )
    assert via_wrapper == via_render


def test_build_prompt_find_qtype_matches_render_prompt():
    """Same parity check for the find branch."""
    from scripts.test_corpora.runner.answer_judge import render_prompt
    from scripts.test_corpora.runner.cross_judge import _build_prompt

    cap = {
        "question": "List Houston emails",
        "answer": "3 found",
        "cited_doc_titles": ["e1.eml"],
    }
    ak = {"count": 87, "all": [], "sample": ["e1.eml", "e2.eml"]}
    via_wrapper = _build_prompt(cap, qtype="find", answer_key=ak)
    via_render = render_prompt(
        question="List Houston emails",
        model_answer="3 found",
        cited="- e1.eml",
        answer_key=ak,
        qtype="find",
    )
    assert via_wrapper == via_render
