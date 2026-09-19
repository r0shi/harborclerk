"""Turn a finished (or stopped) sweep into the dated report a model decision can cite.

`.claude/skills/model-refresh` promotes or retires nothing "without a dated report in docs/reports/ that names
corpus, commit, model, judge and spend". This writes that report from what the run left on disk: metrics.csv,
state.json, spend.json, and the machine preflight's record. It states what was measured and on what machine;
the Reading at the end is for whoever ran it.

    uv run python -m scripts.test_corpora.report --run-dir <workdir>/results/<run_id> --out docs/reports/

One file per run, never overwritten. Exit 0 and the path on stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from scripts.test_corpora.runner import spend

REPO = Path(__file__).resolve().parents[2]
JUDGED_PHASES = {"4", "5"}
# The phases that run a local model, in the order the sweep runs them.
MODEL_PHASES = {
    "2": "Phase 2: smoke",
    "3": "Phase 3: one model at each depth",
    "4": "Phase 4: every model, every question",
    "5": "Phase 5: the two largest, judged",
    "6": "Phase 6: the unified corpus",
}


def suite_commit() -> str:
    def git(*args: str) -> str:
        try:
            done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
        except OSError:  # no git on this machine: the sweep starts anyway
            return ""
        return done.stdout.strip()

    commit = git("rev-parse", "--short=7", "HEAD") or "unknown"
    # Tracked files only: an untracked report left in docs/reports/ by an earlier render is not a change
    # to the suite.
    return commit + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _rows(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / "metrics.csv"
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    # metrics.csv is append-only, and a unit that was re-run has two rows. The last one is the unit's result.
    latest: dict[tuple, dict[str, str]] = {}
    for row in rows:
        latest[(row["phase"], row["corpus"], row["model"], row["question_id"], row["depth"])] = row
    return list(latest.values())


def _mean(values: list[float]) -> str:
    return f"{statistics.fmean(values):.2f}" if values else "n/a"


RAN = ("done", "degraded", "error", "failed")


def model_table(rows: list[dict[str, str]], phase: str, planned: Counter) -> list[str]:
    """One row per corpus and model. Corpora are not pooled: an overlap mean across contracts, mail and
    synthetic memos describes none of them."""
    by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["phase"] == phase:
            by_key[(row["corpus"], row["model"])].append(row)
    keys = sorted(set(by_key) | {(c, m) for (ph, c, m) in planned if ph == phase})
    if not keys:
        return []
    columns = [
        "corpus", "model", "planned", "ran", "done", "degraded", "error", "citation overlap", "entity overlap",
        "median latency (s)", "judged", "pass", "marginal", "fail", "completeness (0-5)",
    ]  # fmt: skip
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for corpus, model in keys:
        units = by_key.get((corpus, model), [])
        status = Counter(u["status"] for u in units)
        done = [u for u in units if u["status"] == "done"]
        judged = [u for u in done if u.get("judge_verdict")]
        verdicts = Counter(u["judge_verdict"] for u in judged)
        latency = [float(u["latency_seconds"]) for u in done if u.get("latency_seconds")]
        ran = sum(status[s] for s in RAN)
        cells = [
            corpus,
            f"`{model}`",
            # state.json is the plan. A run with no state file can only say what it ran.
            planned.get((phase, corpus, model), "n/a") if planned else "n/a",
            ran,
            status["done"],
            status["degraded"],
            status["error"] + status["failed"],
            _mean([float(u["citation_overlap"]) for u in done]),
            _mean([float(u["entity_overlap"]) for u in done]),
            f"{statistics.median(latency):.0f}" if latency else "n/a",
            len(judged),
            verdicts["pass"],
            verdicts["marginal"],
            verdicts["fail"],
            _mean([float(u["judge_completeness"]) for u in judged]),
        ]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    return out


def planned_units(state: dict | None) -> Counter:
    """(phase, corpus, model) -> units the run planned, skipped-before-the-run ones aside (they have their own
    section)."""
    planned: Counter = Counter()
    for unit in (state or {}).get("units", []):
        if (unit.get("error") or "").startswith("skipped before the run"):
            continue
        planned[(str(unit.get("phase")), unit.get("corpus"), unit.get("model"))] += 1
    return planned


def skipped_models(state: dict | None) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for unit in (state or {}).get("units", []):
        if unit.get("status") == "skipped" and (unit.get("error") or "").startswith("skipped before the run"):
            reasons[unit["model"]] = unit["error"].split(": ", 1)[-1]
    return reasons


def run_host(run_dir: Path, preflight: dict | None, rendering_host: str) -> str:
    """The machine that ran the sweep: what the ledger says, else the preflight, else this machine."""
    recorded = (_load(run_dir / spend.LEDGER_NAME) or {}).get("run", {}).get("host") or (preflight or {}).get("host")
    return re.sub(r"[^a-z0-9]+", "-", str(recorded or rendering_host).split(".")[0].lower()).strip("-") or "host"


def render(run_dir: Path, *, preflight: dict | None, today: str, host: str, commit: str) -> str:
    ledger = _load(run_dir / spend.LEDGER_NAME)
    state = _load(run_dir / "state.json")
    rows = _rows(run_dir)
    run_id = (ledger or {}).get("run", {}).get("run_id") or run_dir.name
    # From the plan as well as the rows: a corpus that was planned and produced nothing is still named.
    in_plan = {u.get("corpus") for u in (state or {}).get("units", []) if str(u.get("phase")) in MODEL_PHASES}
    corpora = sorted({r["corpus"] for r in rows} | {c for c in in_plan if c})
    lines = [f"# Benchmark run `{run_id}` on {run_host(run_dir, preflight, host)}, {today}", ""]

    run = (ledger or {}).get("run", {})
    ran_at = run.get("suite_commit")
    commit_line = f"- Suite commit: `{ran_at or commit}`"
    if not ran_at:
        commit_line += " (the checkout this report was rendered from; the run did not record its own)"
    elif ran_at != commit:
        commit_line += f" (the commit that ran the sweep; this report was rendered at `{commit}`)"
    if (ran_at or commit).endswith("-dirty"):
        commit_line += " (**uncommitted changes**: not reproducible from the commit)"
    if run.get("resumed_at_commits"):
        commit_line += f". **Resumed at other commits: {', '.join(run['resumed_at_commits'])}**"
    lines += [
        commit_line,
        f"- Run started: {run.get('started_at', 'not recorded')}",
        f"- Corpora: {', '.join(corpora) or 'none recorded'}",
        "- "
        + (
            spend.header_line(ledger)
            if ledger
            else "Cloud spend: **no spend.json in the run directory**, so spend and judge are not on record."
        ),
    ]
    if preflight is None:
        lines.append(
            "- Machine preflight: **not recorded.** Nothing here says the machine was fit to measure on; the "
            "timings below are not a baseline."
        )
    else:
        problems = [c for c in preflight.get("checks", []) if c["status"] in ("fail", "warn", "skipped")]
        lines.append(
            f"- Machine preflight: **{preflight.get('verdict')}** at {preflight.get('checked_at')}, llama.cpp pin "
            f"{preflight.get('pinned_llama_cpp')}."
            + (" **The timings below are not a baseline.**" if preflight.get("verdict") == "fail" else "")
        )
        lines += [f"  - {c['status']}: {c['name']}: {c['detail']}" for c in problems]
    lines.append("")

    planned = planned_units(state)
    for phase, title in MODEL_PHASES.items():
        table = model_table(rows, phase, planned)
        if table:
            lines += [f"## {title}", "", *table, ""]
    never_ran = sum(n for (ph, _, _), n in planned.items() if ph in MODEL_PHASES) - sum(
        1 for r in rows if r["phase"] in MODEL_PHASES and r["status"] in RAN
    )
    if planned and never_ran > 0:
        lines += [
            f"**{never_ran} planned unit(s) never ran** (pending when the run stopped, or passed over by the circuit "
            "breaker). `planned` against `ran` shows where. A mean over the units that ran says nothing about the "
            "ones that did not.",
            "",
        ]
    if not rows:
        lines += ["## Results", "", "No metrics.csv rows: the run produced no model results.", ""]

    if rows:
        note = (
            "The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and "
            "metrics.csv does not tell them from a true zero. `log.txt` names each one."
        )
        lines += [note, ""]
    unjudged = sum(
        1 for r in rows if r["phase"] in JUDGED_PHASES and r["status"] == "done" and not r.get("judge_verdict")
    )
    if unjudged:
        lines += [
            f"{unjudged} finished unit(s) in the judged phases have no verdict (an unusable baseline, a judge failure, `--no-judge`, or the "
            "spend cap). Their scores are absent from the judged columns, not zero.",
            "",
        ]
    skipped = skipped_models(state)
    if skipped:
        lines += ["## Models this machine did not run", ""]
        lines += [f"- `{model}`: {reason}" for model, reason in sorted(skipped.items())]
        lines.append("")
    if ledger and ledger.get("by_kind"):
        lines += [
            "## Cloud spend",
            "",
            "| call kind | units | calls | input tokens | output tokens | USD |",
            "|---|---|---|---|---|---|",
        ]
        for kind, row in sorted(ledger["by_kind"].items()):
            lines.append(
                f"| {kind} | {row.get('units', 0)} | {row['calls']} | {row['input_tokens']} | {row['output_tokens']} "
                f"| {row['usd']:.4f} |"
            )
        lines.append("")
    lines += [
        "## Reading",
        "",
        "_Written by whoever ran it: what the numbers support, what they do not, and what was not verified._",
        "",
    ]
    return "\n".join(lines)


def write(run_dir: Path, out_dir: Path, *, preflight_path: Path | None) -> Path:
    today = time.strftime("%Y-%m-%d")
    preflight = _load(preflight_path) if preflight_path else _load(run_dir / "preflight.json")
    host = run_host(run_dir, preflight, platform.node())
    text = render(run_dir, preflight=preflight, today=today, host=host, commit=suite_commit())
    out_dir.mkdir(parents=True, exist_ok=True)
    run_slug = re.sub(r"[^a-z0-9]+", "-", run_dir.name.lower()).strip("-")
    path = out_dir / f"{today}-benchmark-{host}-{run_slug}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{today}-benchmark-{host}-{run_slug}-{n}.md"
        n += 1
    path.write_text(text)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--preflight", type=Path, default=None, help="default: <run-dir>/preflight.json")
    args = p.parse_args(argv)
    if not args.run_dir.is_dir():
        sys.stderr.write(f"report: {args.run_dir} is not a directory\n")
        return 2
    print(write(args.run_dir, args.out, preflight_path=args.preflight))
    return 0


if __name__ == "__main__":
    sys.exit(main())
