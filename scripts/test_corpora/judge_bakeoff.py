"""Re-judge a finished run's answers with other judge models, without re-running anything local (#661, #663).

A sweep's judge is one model with one rubric, and changing either makes every earlier score incomparable. This
reads what a run left on disk (the baseline answers and each model's answers), sends the same triples to each
candidate judge, and writes one verdict file per (rubric, judge, repetition, answer). It spends only judge money,
through the same meter and cap as everything else, and it resumes: a verdict already on disk is not bought again.

    uv --project scripts/test_corpora run python -m scripts.test_corpora.judge_bakeoff \\
        --run-dir "$WORKDIR/results/$RUN" --out-dir "$WORKDIR/results/$RUN/bakeoff" \\
        --judges claude-sonnet-5,gpt-5.6-terra --reps 2 --rubric reference --spend-cap-usd 3

It compares judges with each other. Whether any of them is right is a separate question, answered by scoring the
same answers against ground truth the corpus already carries (CUAD's human annotations) and setting the two side
by side: that analysis is not in this file.

What it judges is what the sweep would: units the run finished (`done` in state.json, when the run has one),
with a non-empty answer, against a baseline `baseline_quality_problem` accepts. What it leaves out is counted and
logged by reason. It differs from the sweep in one way: the question is read from the baseline file.

The cap is per --out-dir, which has its own ledger: this is a separate purchase from the run it reads, and the
run's own spend.json is never touched (an --out-dir that is a run directory is refused).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from scripts.test_corpora.runner import spend
from scripts.test_corpora.runner.judge import _extract_json
from scripts.test_corpora.runner.providers.factory import _ANTHROPIC_PREFIXES, _OPENAI_PREFIXES
from scripts.test_corpora.runner.quality import baseline_quality_problem

log = logging.getLogger("judge_bakeoff")

# The rubric the sweep used until #695, frozen here: the variants below are built from it by exact replacement,
# and building them from the live prompt would double-apply them once that prompt carries the change (review of
# #695). "reference" in every report of this tool means this text.
JUDGE_PROMPT = """You are evaluating whether a local LLM's answer reaches the same factual
ground as a Claude baseline answer to the same question.

Question: {question}

Claude baseline answer:
{baseline}

Local model answer:
{model_answer}

Score these dimensions (0–5 each):
- claim_recall: how many factual claims from the baseline appear in the
  model answer (verbatim or paraphrased)?
- claim_precision: are the claims in the model answer supported by the
  baseline (or harmless additions), or are they contradictions?
- entity_recall: does the model answer surface the same named entities
  (people, places, organizations, dates, dollar amounts)?
- completeness: overall coverage of the baseline's territory.

Return JSON only (no prose, no markdown fences):
{{
  "claim_recall": int,
  "claim_precision": int,
  "entity_recall": int,
  "completeness": int,
  "missing_facts": ["..."],
  "extra_facts": ["..."],
  "contradictions": ["..."],
  "verdict": "pass" | "marginal" | "fail"
}}
"""

# The sweep's rubric grades coverage of the baseline, so a correct, short answer scores "marginal" beside a
# reference that said more than the question asked (bench-20260921-0041: both parties named, full citation
# overlap, 3 of 5). This one asks the two questions apart, and takes its verdict from the first.
SPLIT_PROMPT = """You are grading an answer to a question about a collection of documents. A reference answer,
written by a stronger system that searched the same collection, is provided. The reference may say more than the
question asked for, and it may itself be incomplete.

Question: {question}

Reference answer:
{baseline}

Answer to grade:
{model_answer}

Judge two things separately (0-5 each):
- answers_question: does the answer correctly answer what the question asked? Do not penalise brevity, or the
  absence of detail the question did not ask for. Penalise wrong facts, contradictions of the reference, and not
  answering (including saying nothing was found when the reference found it).
- coverage: how much of the reference's relevant content does the answer cover?

Return JSON only (no prose, no markdown fences):
{{
  "answers_question": int,
  "coverage": int,
  "wrong_facts": ["..."],
  "verdict": "pass" | "marginal" | "fail"
}}
The verdict follows answers_question alone: 4-5 pass, 2-3 marginal, 0-1 fail.
"""


def _variant(*changes: tuple[str, str]) -> str:
    """The sweep's prompt with exactly these replacements, each of which must apply once. A rubric test that
    changes four things at once cannot say which of them mattered: the first bake-off's split rubric did."""
    prompt = JUDGE_PROMPT
    for old, new in changes:
        if prompt.count(old) != 1:
            raise ValueError(f"the sweep's judge prompt no longer contains exactly one {old!r}")
        prompt = prompt.replace(old, new)
    return prompt


_NEUTRAL_LABELS = (
    (
        "whether a local LLM's answer reaches the same factual\nground as a Claude baseline answer",
        "whether an answer reaches the same factual\nground as a reference answer",
    ),
    ("Claude baseline answer:\n", "Reference answer:\n"),
    ("Local model answer:\n", "Answer to grade:\n"),
)
_FALLIBLE = (
    "Score these dimensions (0–5 each):",
    "The baseline may say more than the question asked for, and it may itself be incomplete or wrong.\n\n"
    "Score these dimensions (0–5 each):",
)
_THRESHOLDS = (
    '  "verdict": "pass" | "marginal" | "fail"\n}}\n',
    '  "verdict": "pass" | "marginal" | "fail"\n}}\nThe verdict follows completeness: 4-5 pass, 2-3 marginal, 0-1 fail.\n',
)
_ANSWERS_QUESTION = (
    "- completeness: overall coverage of the baseline's territory.\n",
    "- completeness: overall coverage of the baseline's territory.\n"
    "- answers_question: does the model answer correctly answer what the question asked? Do not penalise\n"
    "  brevity, or the absence of detail the question did not ask for. Penalise wrong facts, contradictions of\n"
    "  the baseline, and not answering (including saying nothing was found when the baseline found it).\n",
)
_ANSWERS_QUESTION_JSON = ('  "completeness": int,\n', '  "completeness": int,\n  "answers_question": int,\n')
_VERDICT_FROM_ANSWER = (
    '  "verdict": "pass" | "marginal" | "fail"\n}}\n',
    '  "verdict": "pass" | "marginal" | "fail"\n}}\nThe verdict follows answers_question alone, not completeness.\n',
)

# One change each from the sweep's rubric ("reference"), then "split", the first bake-off's draft, which makes
# all of them at once in other words. `score_field` is what a rubric's verdict is meant to follow.
RUBRICS = {
    "reference": JUDGE_PROMPT,
    "neutral-labels": _variant(*_NEUTRAL_LABELS),
    "fallible-reference": _variant(_FALLIBLE),
    "thresholds": _variant(_THRESHOLDS),
    "answers-question": _variant(_ANSWERS_QUESTION, _ANSWERS_QUESTION_JSON, _VERDICT_FROM_ANSWER),
    "split": SPLIT_PROMPT,
}
SCORE_FIELD = {
    name: "answers_question" if name in ("answers-question", "split") else "completeness" for name in RUBRICS
}


# Claude models that think by themselves when `thinking` is left out. A judge that thinks is another candidate,
# and at 2,000 output tokens it would be cut off mid-thought, so they are run with it off, like the others.
_THINKS_BY_DEFAULT = ("claude-sonnet-5", "claude-opus-5")
# Stop reasons that mean the judge was cut off, not that it finished. Such a record has no verdict to trust.
_TRUNCATED = ("max_tokens", "length")


def load_items(run_dir: Path) -> tuple[list[dict], dict[str, int]]:
    """(what to judge, what was left out and why). One item per response file: a question answered at three
    depths is three answers, and the depth is in the key, or two of them would be skipped as already judged."""
    finished = None
    state_path = run_dir / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        units = state.get("units", state)
        units = units.values() if isinstance(units, dict) else units
        finished = {
            (u["corpus"], u["model"], u["question_id"], u.get("depth") or "standard")
            for u in units
            if isinstance(u, dict) and u.get("status") == "done"
        }
    items, left_out = [], {}

    def skip(reason: str) -> None:
        left_out[reason] = left_out.get(reason, 0) + 1

    for path in sorted((run_dir / "responses").glob("*/*/*.json")):
        record = json.loads(path.read_text())
        depth = record.get("depth") or "standard"
        unit = (record["corpus"], record["model"], record["question_id"], depth)
        if finished is not None and unit not in finished:
            skip("the run did not finish this unit (not `done` in state.json)")
            continue
        result = record.get("result") or {}
        answer = (result.get("answer") or result.get("report") or "").strip()
        if not answer:
            skip("empty answer")
            continue
        baseline_path = run_dir / "baselines" / record["corpus"] / f"{record['question_id']}.json"
        if not baseline_path.exists():
            skip("no baseline")
            continue
        baseline = json.loads(baseline_path.read_text())
        problem = baseline_quality_problem(baseline)
        if problem:
            skip(f"unusable baseline: {problem}")
            continue
        items.append(
            {
                "key": "__".join(unit),
                "model": record["model"],
                "question_id": record["question_id"],
                "depth": depth,
                "question": baseline["question"],
                "baseline": baseline.get("answer") or "",
                "answer": answer,
            }
        )
    return items, left_out


def ask(judge: str, prompt: str) -> tuple[str, str]:
    """(text, stop reason) from `judge`. Each vendor's metered client, so every call is reserved and booked."""
    if judge.startswith(_ANTHROPIC_PREFIXES):
        client = spend.anthropic_client("judge")
        # Thinking is off for the models that would otherwise turn it on by themselves (Sonnet 5 runs adaptive
        # when the field is omitted; Sonnet 4.6 and Haiku 4.5 do not): one deployment to compare, and the older
        # two are what it is compared with. A judge that thinks is a different candidate, not measured here.
        extra = {"thinking": {"type": "disabled"}} if judge.startswith(_THINKS_BY_DEFAULT) else {}
        msg = client.messages.create(
            model=judge, max_tokens=2000, messages=[{"role": "user", "content": prompt}], **extra
        )
        spend.get_meter().count_unit("judge")
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
        return text, str(msg.stop_reason)
    if judge.startswith(_OPENAI_PREFIXES):
        client = spend.openai_client("judge")
        resp = client.chat.completions.create(
            model=judge, max_completion_tokens=6000, messages=[{"role": "user", "content": prompt}]
        )
        spend.get_meter().count_unit("judge")
        choice = resp.choices[0]
        return choice.message.content or "", str(choice.finish_reason)
    raise ValueError(f"{judge!r} is neither a Claude nor an OpenAI model")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--judges", required=True, help="comma-separated model ids")
    p.add_argument("--reps", type=int, default=1, help="repetitions per judge (self-consistency)")
    p.add_argument("--first-rep", type=int, default=0, help="number the repetitions from here")
    p.add_argument("--rubric", choices=sorted(RUBRICS), default="reference")
    p.add_argument("--keys-file", type=Path, help="JSON list of item keys to judge; default every item")
    p.add_argument("--spend-cap-usd", type=float, default=None)
    p.add_argument("--retry-failed", action="store_true", help="buy again the verdicts that could not be read")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if (args.out_dir / "state.json").exists() or args.out_dir.resolve() == args.run_dir.resolve():
        # A run directory has its own ledger. Writing there would resume into the sweep's spend.json, rename its
        # mode, and drop rubric folders among its results.
        log.error("--out-dir %s is a run directory; give the bake-off a directory of its own", args.out_dir)
        return 2
    items, left_out = load_items(args.run_dir)
    if args.keys_file:
        wanted = set(json.loads(args.keys_file.read_text()))
        items = [i for i in items if i["key"] in wanted]
    for reason, count in sorted(left_out.items()):
        log.info("left out, %s: %d", reason, count)
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    def pending(judge: str, rep: int) -> list[tuple[dict, Path]]:
        folder = args.out_dir / args.rubric / judge / f"rep{rep}"
        todo = []
        for item in items:
            out = folder / f"{item['key']}.json"
            # A record without a verdict is kept as a result about that judge, unless the caller asks again.
            if out.exists() and not (args.retry_failed and "verdict" not in json.loads(out.read_text())):
                continue
            todo.append((item, out))
        return todo

    reps = range(args.first_rep, args.first_rep + args.reps)
    try:
        meter = spend.configure(
            ledger_path=args.out_dir / spend.LEDGER_NAME,
            cap_usd=args.spend_cap_usd,
            run_info={"mode": "judge-bakeoff", "source_run": args.run_dir.name},
        )
        # Every judge is priced, and the whole purchase is estimated against the cap, before any of it is bought.
        estimate = meter.require_within_cap(
            [("judge", judge, sum(len(pending(judge, r)) for r in reps)) for judge in judges]
        )
        log.info(
            "%d answers, judges %s, rubric %s, reps %d, estimated USD %.2f; %s",
            len(items),
            judges,
            args.rubric,
            args.reps,
            estimate,
            spend.header_line(meter.snapshot()),
        )
        unparsed = truncated = 0
        for judge in judges:
            for rep in reps:
                for item, out in pending(judge, rep):
                    out.parent.mkdir(parents=True, exist_ok=True)
                    prompt = RUBRICS[args.rubric].format(
                        question=item["question"], baseline=item["baseline"], model_answer=item["answer"]
                    )
                    started = time.time()
                    text, stop = ask(judge, prompt)
                    record = {"judge": judge, "rep": rep, "rubric": args.rubric, "key": item["key"], "stop": stop}
                    try:
                        if stop in _TRUNCATED:
                            raise ValueError(f"cut off ({stop}): whatever JSON is there is not the judge's verdict")
                        record["verdict"] = _extract_json(text)
                    except ValueError as exc:  # json.JSONDecodeError is one: an unparseable judge is a result
                        record["parse_error"] = f"{exc}"[:200]
                        record["raw"] = text[:2000]
                        unparsed += 1
                        truncated += stop in _TRUNCATED
                    record["seconds"] = round(time.time() - started, 1)
                    out.write_text(json.dumps(record, indent=1))
                log.info("%s rep%d done; %s", judge, rep, spend.header_line(meter.snapshot()))
        if unparsed:
            log.warning(
                "%d verdict(s) could not be read (%d cut off); --retry-failed buys them again", unparsed, truncated
            )
    except (spend.SpendError, spend.SpendConfigError) as exc:
        log.error("stopped by the spend cap or its configuration: %s", exc)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
