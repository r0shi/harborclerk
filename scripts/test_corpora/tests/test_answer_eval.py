# scripts/test_corpora/tests/test_answer_eval.py
import json
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_eval import aggregate, load_groundtruth, run
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
