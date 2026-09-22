"""The agreement analysis sets judges beside an answer key. Its arithmetic decides a recommendation, so it is
pinned on cases small enough to do by hand."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

from scripts.test_corpora import judge_agreement as ja
from scripts.test_corpora import judge_bakeoff as jb


def _changed(prompt: str) -> tuple[list[str], list[str]]:
    diff = list(difflib.unified_diff(jb.JUDGE_PROMPT.splitlines(), prompt.splitlines(), lineterm="", n=0))
    lines = [line for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    return [line[1:] for line in lines if line[0] == "-"], [line[1:] for line in lines if line[0] == "+"]


def test_each_rubric_variant_changes_one_thing_in_the_sweeps_prompt():
    """The first bake-off's split rubric changed four things at once, so its effect could not be attributed."""
    assert jb.RUBRICS["reference"] is jb.JUDGE_PROMPT
    removed, added = _changed(jb.RUBRICS["neutral-labels"])
    assert len(removed) == len(added) == 4 and not any("Claude" in line or "local" in line.lower() for line in added)
    assert any("Claude baseline" in line for line in removed)
    removed, added = _changed(jb.RUBRICS["fallible-reference"])
    assert removed == [] and [line for line in added if line] == [
        "The baseline may say more than the question asked for, and it may itself be incomplete or wrong."
    ]
    removed, added = _changed(jb.RUBRICS["thresholds"])
    assert removed == [] and added == ["The verdict follows completeness: 4-5 pass, 2-3 marginal, 0-1 fail."]
    removed, added = _changed(jb.RUBRICS["answers-question"])
    assert removed == [] and len(added) == 5 and '  "answers_question": int,' in added, (
        "three lines of rubric, one of JSON, one of rule"
    )
    assert added[-1] == "The verdict follows answers_question alone, not completeness."
    for name, prompt in jb.RUBRICS.items():
        assert prompt.format(question="Q?", baseline="B.", model_answer="A.").count("Q?") == 1, name
    assert {n for n, f in jb.SCORE_FIELD.items() if f == "answers_question"} == {"answers-question", "split"}


def test_the_reference_rubric_is_frozen_not_the_sweeps_live_prompt():
    """#695 adds answers_question to the sweep's prompt. Built from the live prompt, every variant here would then
    double-apply and "reference" would mean the new rubric in every report of this tool."""
    assert "answers_question" not in jb.JUDGE_PROMPT, "the frozen text is the rubric the sweep used until #695"
    assert jb.JUDGE_PROMPT.count("verdict") == 1, "the frozen text names no rule for the verdict"


def test_a_variant_that_no_longer_applies_says_so(monkeypatch):
    """If someone rewords the sweep's prompt, a variant must not silently become the sweep's prompt again."""
    monkeypatch.setattr(jb, "JUDGE_PROMPT", jb.JUDGE_PROMPT.replace("Score these dimensions", "Rate these"))
    with pytest.raises(ValueError, match="no longer contains exactly one"):
        jb._variant(jb._FALLIBLE)


def test_rank_correlation_by_hand():
    assert ja.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert ja.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Ties take the mean rank: truth ranks 1.5, 1.5, 3.5, 3.5 against 1, 2, 3, 4.
    assert ja.spearman([0, 0, 1, 1], [1, 2, 3, 4]) == pytest.approx(0.894427, abs=1e-6)
    assert ja.spearman([1, 1, 1], [1, 2, 3]) is None and ja.spearman([1, 2], [1, 2]) is None


def _truth(rows: list[tuple[str, str, float]]) -> dict:
    return {key: {"truth": t, "question_id": q, "model": key.split("__")[1], "kind": "fact"} for key, q, t in rows}


def test_agreement_counts_right_answers_passed_and_wrong_answers_failed():
    truth = _truth(
        [
            ("c__m1__q1__s", "q1", 1.0),
            ("c__m2__q1__s", "q1", 0.0),
            ("c__m1__q2__s", "q2", 0.9),
            ("c__m2__q2__s", "q2", 0.5),
            ("c__m1__q3__s", "q3", 0.1),
        ]
    )
    verdicts = {
        "c__m1__q1__s": {"completeness": 5, "verdict": "pass"},
        "c__m2__q1__s": {"completeness": 1, "verdict": "fail"},
        "c__m1__q2__s": {"completeness": 3, "verdict": "marginal"},  # right by the key, only marginal to the judge
        "c__m2__q2__s": {"completeness": 3, "verdict": "marginal"},  # neither right nor wrong: not a decision
        "c__m1__q3__s": {"completeness": 4, "verdict": "pass"},  # wrong by the key, passed
        "c__m9__q9__s": {"completeness": 5, "verdict": "pass"},  # no truth for it: ignored
        "c__m2__q3__s": None,  # unreadable
    }
    out = ja.agreement(truth, verdicts, "reference", resamples=200)
    assert (out["judged"], out["unreadable"], out["with_truth"], out["questions"]) == (7, 1, 5, 3)
    assert out["right_answers"] == {"n": 2, "pass_marginal_fail": [1, 1, 0]}
    assert out["wrong_answers"] == {"n": 2, "pass_marginal_fail": [1, 0, 1]}
    assert (out["correct_calls"], out["decided"]) == (2, 4)
    assert out["score_field"] == "completeness"


def test_a_rubric_is_correlated_on_the_score_its_verdict_follows():
    truth = _truth([(f"c__m__q{i}__s", f"q{i}", t) for i, t in enumerate([0.0, 0.3, 0.6, 1.0])])
    verdicts = {
        k: {"completeness": c, "answers_question": a, "verdict": "pass"}
        for k, c, a in zip(truth, [5, 4, 3, 2], [1, 2, 3, 4], strict=True)
    }
    assert ja.agreement(truth, verdicts, "reference", resamples=50)["spearman"] == pytest.approx(-1.0)
    assert ja.agreement(truth, verdicts, "split", resamples=50)["spearman"] == pytest.approx(1.0)


def test_the_interval_resamples_questions_not_answers():
    """Forty answers to two questions are two draws. An interval from resampling answers would be narrow; from
    resampling questions it cannot be computed at all, which is the honest result."""
    two = [("q1", 1.0, 5.0)] * 20 + [("q2", 0.0, 1.0)] * 20
    assert ja.question_bootstrap(two, resamples=200, seed=1) is None
    many = [(f"q{i}", i / 10, float(i % 6)) for i in range(10) for _ in range(4)]
    low, high = ja.question_bootstrap(many, resamples=400, seed=1)
    assert -1.0 <= low <= high <= 1.0 and high - low > 0.2, "ten questions leave a wide interval, and it says so"
    assert ja.question_bootstrap(many, resamples=400, seed=1) == (low, high), "seeded: a report can be regenerated"


def test_end_to_end_from_a_run_directory(tmp_path, capsys):
    run = tmp_path / "results" / "r1"
    (run / "baselines" / "cuad").mkdir(parents=True)
    key = {}
    for i, (law, answers) in enumerate(
        [
            ("Texas", {"m1": "Texas law governs.", "m2": "New York."}),
            ("Ohio", {"m1": "The State of Ohio.", "m2": "Ohio"}),
            ("Utah", {"m1": "Nevada", "m2": "I do not know."}),
        ],
        start=1,
    ):
        qid = f"cuad-key-{i}"
        key[qid] = {"kind": "fact", "all_of": [[law]]}
        (run / "baselines" / "cuad" / f"{qid}.json").write_text(
            json.dumps({"question": f"law {i}?", "answer": f"{law}."})
        )
        for model, answer in answers.items():
            folder = run / "responses" / "cuad" / model
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{qid}__standard.json").write_text(
                json.dumps(
                    {
                        "corpus": "cuad",
                        "model": model,
                        "question_id": qid,
                        "depth": "standard",
                        "result": {"answer": answer, "citations": []},
                    }
                )
            )
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps(key))
    truth = ja.truth_scores(run, key)
    assert {k: v["truth"] for k, v in truth.items()} == {
        "cuad__m1__cuad-key-1__standard": 1.0,
        "cuad__m1__cuad-key-2__standard": 1.0,
        "cuad__m1__cuad-key-3__standard": 0.0,
        "cuad__m2__cuad-key-1__standard": 0.0,
        "cuad__m2__cuad-key-2__standard": 1.0,
        "cuad__m2__cuad-key-3__standard": 0.0,
    }
    folder = tmp_path / "bakeoff" / "reference" / "judge-x" / "rep0"
    folder.mkdir(parents=True)
    for item_key, t in truth.items():
        verdict = {"completeness": 5 if t["truth"] else 0, "verdict": "pass" if t["truth"] else "fail"}
        (folder / f"{item_key}.json").write_text(
            json.dumps({"rubric": "reference", "judge": "judge-x", "rep": 0, "key": item_key, "verdict": verdict})
        )
    out = tmp_path / "agreement.json"
    assert (
        ja.main(
            [
                "--run-dir",
                str(run),
                "--bakeoff-dir",
                str(tmp_path / "bakeoff"),
                "--key",
                str(key_path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    summary = json.loads(out.read_text())["summary"]
    assert summary["answer_key_mean_by_model"] == {"m1": 0.667, "m2": 0.333}
    row = summary["rows"][0]
    assert row["spearman"] == pytest.approx(1.0) and (row["correct_calls"], row["decided"]) == (6, 6)
    assert "| reference | `judge-x` | 0 | 1.00 |" in capsys.readouterr().out
    assert Path(out).exists()
