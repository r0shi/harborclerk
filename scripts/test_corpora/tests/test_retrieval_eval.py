"""Tests for the --mode retrieval-eval fast path.

The HTTP-calling functions are tested with a stub search callable so these
tests don't need a running HC. The pure-data functions (load_baselines,
score_one, aggregate, compute_diff) are all unit-testable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.test_corpora.runner.retrieval_eval import (
    BaselineRecord,
    EvalRow,
    aggregate,
    compute_diff,
    load_baselines,
    score_one,
)

# ── load_baselines ──


def _write_baseline(baselines_dir: Path, corpus: str, question_id: str, **fields) -> None:
    d = baselines_dir / corpus
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "question_id": question_id,
        "question": f"What about {question_id}?",
        "answer": "an answer",
        "cited_doc_ids": ["doc-a", "doc-b"],
        "tool_call_count": 3,
        "elapsed_seconds": 1.0,
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-05-17T00:00:00Z",
        **fields,
    }
    (d / f"{question_id}.json").write_text(json.dumps(payload))


def test_load_baselines_reads_all_corpora_when_filter_is_none(tmp_path):
    baselines = tmp_path / "baselines"
    _write_baseline(baselines, "cuad", "cuad-ask-1")
    _write_baseline(baselines, "enron", "enron-research-1")
    _write_baseline(baselines, "synthetic", "syn-1")

    records = load_baselines(baselines, corpora=None, limit_per_corpus=None)

    by_corpus = {r.corpus for r in records}
    assert by_corpus == {"cuad", "enron", "synthetic"}
    assert len(records) == 3


def test_load_baselines_corpus_filter_restricts(tmp_path):
    baselines = tmp_path / "baselines"
    _write_baseline(baselines, "cuad", "cuad-1")
    _write_baseline(baselines, "enron", "enron-1")

    records = load_baselines(baselines, corpora={"cuad"}, limit_per_corpus=None)

    assert {r.corpus for r in records} == {"cuad"}
    assert len(records) == 1


def test_load_baselines_limit_per_corpus_caps_count(tmp_path):
    baselines = tmp_path / "baselines"
    for i in range(5):
        _write_baseline(baselines, "cuad", f"cuad-{i}")

    records = load_baselines(baselines, corpora=None, limit_per_corpus=2)

    assert len(records) == 2
    assert all(r.corpus == "cuad" for r in records)


def test_load_baselines_returns_baseline_record_with_question_text(tmp_path):
    baselines = tmp_path / "baselines"
    _write_baseline(
        baselines,
        "cuad",
        "cuad-1",
        question="Specific question text here.",
        cited_doc_ids=["x", "y"],
    )

    [rec] = load_baselines(baselines, corpora=None, limit_per_corpus=None)

    assert isinstance(rec, BaselineRecord)
    assert rec.corpus == "cuad"
    assert rec.question_id == "cuad-1"
    assert rec.question_text == "Specific question text here."
    assert rec.cited_doc_ids == ["x", "y"]


def test_load_baselines_skips_baselines_with_no_citations(tmp_path):
    """An empty cited_doc_ids list means the baseline can't grade retrieval.
    These are surfaced by the existing baseline_quality_problem check; the
    eval should skip them so the metrics aren't polluted with 0.0 rows that
    look like regressions."""
    baselines = tmp_path / "baselines"
    _write_baseline(baselines, "cuad", "cuad-1", cited_doc_ids=["a"])
    _write_baseline(baselines, "cuad", "cuad-empty", cited_doc_ids=[])

    records = load_baselines(baselines, corpora=None, limit_per_corpus=None)

    assert [r.question_id for r in records] == ["cuad-1"]


def test_load_baselines_handles_missing_baselines_dir(tmp_path):
    """A missing baselines dir is not a crash — it's just zero records.

    Lets retrieval-eval mode report 'no baselines yet' instead of blowing
    up if invoked before phase 1 has populated the run dir.
    """
    records = load_baselines(tmp_path / "nope", corpora=None, limit_per_corpus=None)
    assert records == []


# ── score_one ──


def test_score_one_perfect_retrieval():
    baseline = BaselineRecord(
        corpus="cuad",
        question_id="q1",
        question_text="?",
        cited_doc_ids=["a", "b"],
    )
    ranked = ["a", "b", "x", "y", "z"]
    row = score_one(baseline, ranked, ks=[5, 10])
    assert row.recall_at_k == {5: 1.0, 10: 1.0}
    assert row.reciprocal_rank == 1.0
    assert row.ndcg_at_10 == 1.0
    assert row.top_doc_id == "a"


def test_score_one_no_hits_zero_metrics():
    baseline = BaselineRecord(
        corpus="cuad",
        question_id="q1",
        question_text="?",
        cited_doc_ids=["a"],
    )
    row = score_one(baseline, ["x", "y", "z"], ks=[5, 10])
    assert row.recall_at_k == {5: 0.0, 10: 0.0}
    assert row.reciprocal_rank == 0.0
    assert row.ndcg_at_10 == 0.0
    assert row.top_doc_id == "x"


def test_score_one_records_returned_ranked_and_baseline_cites():
    """For the per-question CSV. Lets the operator inspect WHICH docs the
    new pipeline surfaced vs what the baseline expected, without re-running."""
    baseline = BaselineRecord(corpus="cuad", question_id="q1", question_text="?", cited_doc_ids=["a", "b"])
    row = score_one(baseline, ["x", "a", "y"], ks=[5])
    assert row.baseline_cited_doc_ids == ["a", "b"]
    assert row.returned_doc_ids == ["x", "a", "y"]


def test_score_one_empty_ranked_zero_metrics_no_top_doc():
    baseline = BaselineRecord(corpus="cuad", question_id="q1", question_text="?", cited_doc_ids=["a"])
    row = score_one(baseline, [], ks=[5, 10])
    assert row.recall_at_k == {5: 0.0, 10: 0.0}
    assert row.reciprocal_rank == 0.0
    assert row.ndcg_at_10 == 0.0
    assert row.top_doc_id is None


# ── aggregate ──


def _row(corpus, q, recall, rr, ndcg):
    return EvalRow(
        corpus=corpus,
        question_id=q,
        question_text="?",
        recall_at_k={5: recall, 10: recall, 20: recall},
        reciprocal_rank=rr,
        ndcg_at_10=ndcg,
        top_doc_id="x",
        baseline_cited_doc_ids=["a"],
        returned_doc_ids=["x"],
    )


def test_aggregate_overall_means():
    rows = [
        _row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0),
        _row("cuad", "q2", recall=0.0, rr=0.0, ndcg=0.0),
        _row("enron", "q1", recall=0.5, rr=0.5, ndcg=0.5),
    ]
    summary = aggregate(rows, ks=[5, 10, 20])
    overall = summary["overall"]
    assert overall["n_questions"] == 3
    assert overall["recall_at_k"][10] == pytest.approx(0.5)
    assert overall["mrr"] == pytest.approx(0.5)
    assert overall["ndcg_at_10"] == pytest.approx(0.5)


def test_aggregate_per_corpus_means():
    rows = [
        _row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0),
        _row("cuad", "q2", recall=0.0, rr=0.0, ndcg=0.0),
        _row("enron", "q1", recall=0.4, rr=0.4, ndcg=0.4),
    ]
    summary = aggregate(rows, ks=[5, 10, 20])
    assert summary["per_corpus"]["cuad"]["n_questions"] == 2
    assert summary["per_corpus"]["cuad"]["recall_at_k"][10] == pytest.approx(0.5)
    assert summary["per_corpus"]["enron"]["recall_at_k"][10] == pytest.approx(0.4)


def test_aggregate_empty_rows_returns_zero_n():
    summary = aggregate([], ks=[5, 10])
    assert summary["overall"]["n_questions"] == 0
    assert summary["per_corpus"] == {}


# ── compute_diff ──


def test_compute_diff_detects_regression():
    current = [_row("cuad", "q1", recall=0.5, rr=0.5, ndcg=0.5)]
    prior = [_row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0)]
    diff = compute_diff(current, prior, regression_threshold=0.05)
    assert len(diff["regressions"]) == 1
    reg = diff["regressions"][0]
    assert reg["corpus"] == "cuad"
    assert reg["question_id"] == "q1"
    assert reg["recall_at_10_delta"] == pytest.approx(-0.5)


def test_compute_diff_ignores_below_threshold_movements():
    current = [_row("cuad", "q1", recall=0.95, rr=1.0, ndcg=1.0)]
    prior = [_row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0)]
    diff = compute_diff(current, prior, regression_threshold=0.10)
    assert diff["regressions"] == []


def test_compute_diff_lists_improvements():
    current = [_row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0)]
    prior = [_row("cuad", "q1", recall=0.0, rr=0.0, ndcg=0.0)]
    diff = compute_diff(current, prior, regression_threshold=0.05)
    assert len(diff["improvements"]) == 1
    assert diff["improvements"][0]["recall_at_10_delta"] == pytest.approx(1.0)


def test_compute_diff_reports_overall_deltas():
    current = [
        _row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0),
        _row("cuad", "q2", recall=0.5, rr=0.5, ndcg=0.5),
    ]
    prior = [
        _row("cuad", "q1", recall=0.5, rr=0.5, ndcg=0.5),
        _row("cuad", "q2", recall=0.5, rr=0.5, ndcg=0.5),
    ]
    diff = compute_diff(current, prior, regression_threshold=0.05)
    assert diff["overall_deltas"]["recall_at_10"] == pytest.approx(0.25)
    assert diff["overall_deltas"]["mrr"] == pytest.approx(0.25)


def test_compute_diff_handles_questions_only_in_current():
    """A question added to current with no prior counterpart is reported as
    'new', not as a regression."""
    current = [
        _row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0),
        _row("cuad", "q-new", recall=0.5, rr=0.5, ndcg=0.5),
    ]
    prior = [_row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0)]
    diff = compute_diff(current, prior, regression_threshold=0.05)
    assert diff["new_questions"] == [{"corpus": "cuad", "question_id": "q-new"}]
    assert diff["regressions"] == []


def test_compute_diff_handles_questions_only_in_prior():
    """A question removed from current is reported as 'dropped', not noise."""
    current = [_row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0)]
    prior = [
        _row("cuad", "q1", recall=1.0, rr=1.0, ndcg=1.0),
        _row("cuad", "q-gone", recall=0.5, rr=0.5, ndcg=0.5),
    ]
    diff = compute_diff(current, prior, regression_threshold=0.05)
    assert diff["dropped_questions"] == [{"corpus": "cuad", "question_id": "q-gone"}]
