"""How well do a bake-off's judges agree with an answer key? (#661)

    uv --project scripts/test_corpora run python -m scripts.test_corpora.judge_agreement \\
        --run-dir "$WORKDIR/results/$RUN" --bakeoff-dir "$WORKDIR/results/$RUN/bakeoff" \\
        --key scripts/test_corpora/questions/keyed/cuad.key.json --out "$WORKDIR/results/$RUN/agreement.json"

Reads the answers a run left, scores each against the key (`runner/answer_key.py`), reads every verdict
`judge_bakeoff` bought, and sets the two side by side per (rubric, judge, repetition):

- rank correlation between the truth score and the score the rubric's verdict is meant to follow;
- as a decision: of the answers the key calls right (>= RIGHT), how many passed; of those it calls wrong
  (<= WRONG), how many failed;
- an interval for the correlation from resampling QUESTIONS, not answers. Four models answering one question are
  one draw of that question's difficulty; resampling answers treats them as four and gives an interval that is
  too narrow, which the first bake-off's report did.

It buys nothing and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

from scripts.test_corpora.judge_bakeoff import SCORE_FIELD, load_items
from scripts.test_corpora.runner.answer_key import score as key_score

RIGHT, WRONG = 0.8, 0.2
VERDICTS = ("pass", "marginal", "fail")


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation with ties averaged; None where it is undefined (under three pairs, or no spread)."""
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else None


def truth_scores(run_dir: Path, key: dict) -> dict[str, dict]:
    """item key -> {"truth", "model", "question_id", ...detail} for every judged-able answer the key covers."""
    items, _ = load_items(run_dir)
    out = {}
    for item in items:
        entry = key.get(item["question_id"])
        if entry is None:
            continue
        detail = key_score(entry, item["answer"])  # the text alone: an answer's citations are its retrieval hits
        out[item["key"]] = {
            "truth": detail["score"],
            "model": item["model"],
            "question_id": item["question_id"],
            "kind": entry["kind"],
            **detail,
        }
    return out


def read_verdicts(bakeoff_dir: Path) -> dict[tuple[str, str, int], dict[str, dict | None]]:
    runs: dict[tuple[str, str, int], dict[str, dict | None]] = {}
    for path in sorted(bakeoff_dir.glob("*/*/rep*/*.json")):
        record = json.loads(path.read_text())
        runs.setdefault((record["rubric"], record["judge"], record["rep"]), {})[record["key"]] = record.get("verdict")
    return runs


def question_bootstrap(pairs: list[tuple[str, float, float]], resamples: int, seed: int) -> tuple[float, float] | None:
    """95% interval for Spearman over (question, truth, judged) triples, resampling questions with replacement."""
    by_question: dict[str, list[tuple[float, float]]] = {}
    for question, truth, judged in pairs:
        by_question.setdefault(question, []).append((truth, judged))
    questions = sorted(by_question)
    if len(questions) < 3:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        drawn = [pair for q in (rng.choice(questions) for _ in questions) for pair in by_question[q]]
        rho = spearman([t for t, _ in drawn], [j for _, j in drawn])
        if rho is not None:
            values.append(rho)
    if len(values) < resamples // 2:
        return None
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def agreement(truth: dict[str, dict], verdicts: dict[str, dict | None], rubric: str, resamples: int = 2000) -> dict:
    field = SCORE_FIELD[rubric]
    usable = {k: v for k, v in verdicts.items() if k in truth and v and field in v and v.get("verdict") in VERDICTS}
    pairs = [(truth[k]["question_id"], truth[k]["truth"], float(v[field])) for k, v in usable.items()]
    right = [k for k in usable if truth[k]["truth"] >= RIGHT]
    wrong = [k for k in usable if truth[k]["truth"] <= WRONG]

    def counts(keys: list[str]) -> list[int]:
        return [sum(1 for k in keys if usable[k]["verdict"] == verdict) for verdict in VERDICTS]

    r, w = counts(right), counts(wrong)
    return {
        "score_field": field,
        "judged": len(verdicts),
        "unreadable": sum(1 for v in verdicts.values() if not v),
        "with_truth": len(usable),
        "questions": len({q for q, _, _ in pairs}),
        "spearman": spearman([t for _, t, _ in pairs], [j for _, _, j in pairs]),
        "spearman_95_by_question": question_bootstrap(pairs, resamples, seed=11),
        "right_answers": {"n": len(right), "pass_marginal_fail": r},
        "wrong_answers": {"n": len(wrong), "pass_marginal_fail": w},
        "correct_calls": r[0] + w[2],
        "decided": len(right) + len(wrong),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--bakeoff-dir", required=True, type=Path)
    p.add_argument("--key", required=True, type=Path)
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    truth = truth_scores(args.run_dir, json.loads(args.key.read_text()))
    rows = []
    for (rubric, judge, rep), verdicts in sorted(read_verdicts(args.bakeoff_dir).items()):
        rows.append({"rubric": rubric, "judge": judge, "rep": rep, **agreement(truth, verdicts, rubric)})
    by_model: dict[str, list[float]] = {}
    for t in truth.values():
        by_model.setdefault(t["model"], []).append(t["truth"])
    summary = {
        "answers_with_truth": len(truth),
        "answer_key_mean_by_model": {m: round(statistics.fmean(v), 3) for m, v in sorted(by_model.items())},
        "right_at": RIGHT,
        "wrong_at": WRONG,
        "rows": rows,
    }
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "truth": truth}, indent=1))
    print(f"{len(truth)} answers with a truth score; mean by model: {summary['answer_key_mean_by_model']}")
    print(
        "| rubric | judge | rep | rho | 95% by question | right: pass/marg/fail | wrong: pass/marg/fail | correct calls |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        rho = "n/a" if row["spearman"] is None else f"{row['spearman']:.2f}"
        ci = row["spearman_95_by_question"]
        ci_text = "n/a" if ci is None else f"{ci[0]:.2f} to {ci[1]:.2f}"
        r, w = row["right_answers"]["pass_marginal_fail"], row["wrong_answers"]["pass_marginal_fail"]
        print(
            f"| {row['rubric']} | `{row['judge']}` | {row['rep']} | {rho} | {ci_text} | {r[0]} / {r[1]} / {r[2]} "
            f"| {w[0]} / {w[1]} / {w[2]} | {row['correct_calls']} of {row['decided']} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
