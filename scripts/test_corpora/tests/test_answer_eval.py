# scripts/test_corpora/tests/test_answer_eval.py
import json
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.runner.answer_eval import aggregate, compute_coverage, load_groundtruth, run
from scripts.test_corpora.runner.answer_judge import AnswerVerdict


def test_load_groundtruth(tmp_path: Path):
    gt = tmp_path / "cuad.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "cuad",
                "items": [
                    {
                        "id": "g1",
                        "question": "q1",
                        "clause_category": "Governing Law",
                        "gold_doc": "AcmeCo",
                        "answer_key": "Delaware",
                        "type": "lookup",
                    },
                    {
                        "id": "g2",
                        "question": "q2",
                        "clause_category": "Most Favored Nation",
                        "gold_doc": "BetaCo",
                        "answer_key": None,
                        "type": "negative",
                    },
                ],
            }
        )
    )
    items = load_groundtruth(gt)
    assert [i.id for i in items] == ["g1", "g2"]
    assert items[0].answer_key == "Delaware"
    assert items[1].answer_key is None and items[1].type == "negative"


def test_aggregate_means_overall_and_by_type():
    rows = [
        ("g1", "lookup", AnswerVerdict(5, 4, 5, "")),
        ("g2", "lookup", AnswerVerdict(3, 2, 3, "")),
        ("g3", "negative", AnswerVerdict(5, 5, 5, "")),
    ]
    summary = aggregate(rows)
    assert summary["overall"]["n"] == 3
    assert summary["overall"]["correctness"] == 13 / 3
    assert summary["by_type"]["lookup"]["n"] == 2
    assert summary["by_type"]["negative"]["correctness"] == 5.0


def test_run_reuses_captures_and_verdicts_by_default(tmp_path: Path):
    """run() reuses an existing capture+verdict; --refresh re-runs the model,
    --rejudge re-runs the judge. Captures/verdicts are keyed by corpus/model/id
    (not by label), so they carry across runs with different labels."""
    gt = tmp_path / "cuad.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "cuad",
                "items": [
                    {
                        "id": "g1",
                        "question": "q1",
                        "clause_category": "Governing Law",
                        "gold_doc": "DocA",
                        "answer_key": "Delaware",
                        "type": "lookup",
                    },
                ],
            }
        )
    )

    captures = {"n": 0}

    def fake_capture(item):
        captures["n"] += 1
        return {"answer": "Delaware.", "cited_doc_titles": ["DocA"], "tool_transcript": []}

    judged = {"n": 0}

    class FakeJudge:
        def judge_answer(self, **kw):
            judged["n"] += 1
            return AnswerVerdict(5, 5, 5, "ok")

    common = dict(workdir=tmp_path, corpus="cuad", model="m1", api_base="http://x", insecure=True, groundtruth_path=gt)

    rc = run(**common, label="r1", refresh=False, rejudge=False, capture_fn=fake_capture, judge=FakeJudge())
    assert rc == 0 and captures["n"] == 1 and judged["n"] == 1

    # second run, different label: capture + verdict both reused
    run(**common, label="r2", refresh=False, rejudge=False, capture_fn=fake_capture, judge=FakeJudge())
    assert captures["n"] == 1 and judged["n"] == 1

    # --refresh re-runs the model (and re-judges the fresh capture)
    run(**common, label="r3", refresh=True, rejudge=False, capture_fn=fake_capture, judge=FakeJudge())
    assert captures["n"] == 2 and judged["n"] == 2

    # --rejudge re-runs only the judge
    run(**common, label="r4", refresh=False, rejudge=True, capture_fn=fake_capture, judge=FakeJudge())
    assert captures["n"] == 2 and judged["n"] == 3

    summary = json.loads((tmp_path / "answer-eval" / "reports" / "r4" / "summary.json").read_text())
    assert summary["overall"]["n"] == 1


def test_run_handles_empty_groundtruth(tmp_path: Path):
    """An empty ground-truth set yields a zeroed report, not a KeyError crash."""
    gt = tmp_path / "cuad.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "cuad", "items": []}))

    def no_capture(item):
        raise AssertionError("capture_fn must not be called for an empty ground-truth set")

    class FakeJudge:
        def judge_answer(self, **kw):
            raise AssertionError("judge must not be called for an empty ground-truth set")

    rc = run(
        workdir=tmp_path,
        corpus="cuad",
        model="m1",
        label="empty",
        api_base="http://x",
        refresh=False,
        rejudge=False,
        insecure=True,
        groundtruth_path=gt,
        capture_fn=no_capture,
        judge=FakeJudge(),
    )
    assert rc == 0
    summary = json.loads((tmp_path / "answer-eval" / "reports" / "empty" / "summary.json").read_text())
    assert summary["overall"] == {"n": 0, "correctness": 0.0, "groundedness": 0.0, "completeness": 0.0}


def test_run_rejudges_when_verdict_file_is_corrupt(tmp_path: Path):
    """A good capture plus a corrupt verdict file: the capture is reused, but the
    unreadable verdict is discarded and re-judged rather than crashing."""
    gt = tmp_path / "cuad.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "cuad",
                "items": [
                    {
                        "id": "g1",
                        "question": "q1",
                        "clause_category": "Governing Law",
                        "gold_doc": "DocA",
                        "answer_key": "Delaware",
                        "type": "lookup",
                    }
                ],
            }
        )
    )
    cap_dir = tmp_path / "answer-eval" / "captures" / "cuad" / "m1"
    ver_dir = tmp_path / "answer-eval" / "verdicts" / "cuad" / "m1"
    cap_dir.mkdir(parents=True)
    ver_dir.mkdir(parents=True)
    (cap_dir / "g1.json").write_text(json.dumps({"answer": "Delaware.", "cited_doc_titles": [], "tool_transcript": []}))
    (ver_dir / "g1.json").write_text("{ this is not valid json")

    captures = {"n": 0}

    def counting_capture(item):
        captures["n"] += 1
        return {"answer": "x", "cited_doc_titles": [], "tool_transcript": []}

    judged = {"n": 0}

    class FakeJudge:
        def judge_answer(self, **kw):
            judged["n"] += 1
            return AnswerVerdict(4, 4, 4, "re-judged")

    rc = run(
        workdir=tmp_path,
        corpus="cuad",
        model="m1",
        label="rj",
        api_base="http://x",
        refresh=False,
        rejudge=False,
        insecure=True,
        groundtruth_path=gt,
        capture_fn=counting_capture,
        judge=FakeJudge(),
    )
    assert rc == 0
    assert captures["n"] == 0  # the good capture was reused
    assert judged["n"] == 1  # the corrupt verdict was discarded and re-judged
    assert json.loads((ver_dir / "g1.json").read_text())["correctness"] == 4


def test_compute_coverage_negative_clean_decline():
    """find-negative: truth=[], cited=[] — citing nothing is correct (5)."""
    assert compute_coverage([], []) == 5


def test_compute_coverage_negative_penalizes_unique_false_positives():
    """find-negative: each UNIQUE cited doc costs a point, floor 0. Duplicates
    don't compound — repeating the same wrong claim is still one wrong claim."""
    assert compute_coverage(["x"], []) == 4
    assert compute_coverage(["x", "y", "z"], []) == 2
    assert compute_coverage(["a", "b", "c", "d", "e"], []) == 0
    assert compute_coverage(["a"] * 10, []) == 4  # 10 copies of 'a' → 1 unique → 5-1=4
    assert compute_coverage(["a", "b", "a", "b"], []) == 3  # 2 unique → 5-2=3


def test_compute_coverage_exact_match():
    """find: cited == truth -> 5."""
    assert compute_coverage(["a", "b", "c"], ["a", "b", "c"]) == 5


def test_compute_coverage_partial_overlap_uses_banker_rounding():
    """find: round(overlap / len(truth) * 5). Python 3's round is banker's."""
    # 1/2 = 0.5 -> 2.5 -> 2 (banker's: rounds to even)
    assert compute_coverage(["a"], ["a", "b"]) == 2
    # 2/4 = 0.5 -> 2.5 -> 2
    assert compute_coverage(["a", "b"], ["a", "b", "c", "d"]) == 2
    # 3/4 = 0.75 -> 3.75 -> 4
    assert compute_coverage(["a", "b", "c"], ["a", "b", "c", "d"]) == 4


def test_compute_coverage_over_cite_does_not_penalize():
    """find: extras in cited beyond truth don't reduce coverage — coverage is
    recall (|overlap| / |truth|), not precision."""
    assert compute_coverage(["a", "b", "x", "y"], ["a", "b"]) == 5


def test_load_groundtruth_validates_find_answer_key_is_a_dict(tmp_path: Path):
    """find items must carry a dict answer_key, not a string."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "enron",
                "items": [
                    {
                        "id": "find-bad",
                        "question": "Find x.",
                        "clause_category": "n/a",
                        "gold_doc": "(see answer_key.all)",
                        "answer_key": "wrong shape — should be a dict",
                        "type": "find",
                    },
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="find item .* answer_key must be a dict"):
        load_groundtruth(gt)


def test_load_groundtruth_validates_find_answer_key_required_keys(tmp_path: Path):
    """find item's answer_key must include count, all, and sample."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "enron",
                "items": [
                    {
                        "id": "find-incomplete",
                        "question": "Find x.",
                        "clause_category": "n/a",
                        "gold_doc": "(see answer_key.all)",
                        "answer_key": {"count": 5},
                        "type": "find",
                    },
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="find item .* missing required answer_key keys"):
        load_groundtruth(gt)


def test_load_groundtruth_accepts_well_formed_find_item(tmp_path: Path):
    """A valid find item with the {count, all, sample} answer_key loads cleanly."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "enron",
                "items": [
                    {
                        "id": "find-x",
                        "question": "Find x.",
                        "clause_category": "n/a",
                        "gold_doc": "(see answer_key.all)",
                        "answer_key": {"count": 2, "all": ["a.eml", "b.eml"], "sample": ["a.eml"]},
                        "type": "find",
                    },
                ],
            }
        )
    )
    items = load_groundtruth(gt)
    assert items[0].answer_key == {"count": 2, "all": ["a.eml", "b.eml"], "sample": ["a.eml"]}
    assert items[0].type == "find"


def test_run_overrides_completeness_for_find_items_with_compute_coverage(tmp_path: Path):
    """For find items, run() ignores the judge's completeness and uses the
    deterministic set-overlap score instead. source['completeness'] is set."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(
        yaml.safe_dump(
            {
                "corpus": "enron",
                "items": [
                    {
                        "id": "find-x",
                        "question": "Find x.",
                        "clause_category": "n/a",
                        "gold_doc": "(see answer_key.all)",
                        "answer_key": {
                            "count": 4,
                            "all": ["a.eml", "b.eml", "c.eml", "d.eml"],
                            "sample": ["a.eml", "b.eml"],
                        },
                        "type": "find",
                    },
                ],
            }
        )
    )

    def capture_two_of_four(item):
        # Model cited 2 of 4 truth docs -> coverage = round(2/4 * 5) = round(2.5) = 2
        return {"answer": "Two relevant", "cited_doc_titles": ["a.eml", "c.eml"], "tool_transcript": []}

    class FakeJudge:
        def judge_answer(self, **kw):
            # Judge would say 5 — but for find items the runner must override.
            return AnswerVerdict(correctness=5, groundedness=5, completeness=5, rationale="judge-said-5")

    rc = run(
        workdir=tmp_path,
        corpus="enron",
        model="m1",
        label="ov",
        api_base="http://x",
        refresh=False,
        rejudge=False,
        insecure=True,
        groundtruth_path=gt,
        capture_fn=capture_two_of_four,
        judge=FakeJudge(),
    )
    assert rc == 0

    persisted = json.loads((tmp_path / "answer-eval" / "verdicts" / "enron" / "m1" / "find-x.json").read_text())
    assert persisted["correctness"] == 5  # judge's value carried through
    assert persisted["groundedness"] == 5  # ditto
    assert persisted["completeness"] == 2  # overridden by compute_coverage
    assert persisted["source"]["completeness"] == "deterministic"

    summary = json.loads((tmp_path / "answer-eval" / "reports" / "ov" / "summary.json").read_text())
    # by_type now has a "find" bucket
    assert "find" in summary["by_type"]
    assert summary["by_type"]["find"]["completeness"] == 2.0
