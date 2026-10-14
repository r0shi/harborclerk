"""The dated report a model decision cites: it names corpus, commit, model, judge and spend, and says
whether the machine was fit to measure on."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.test_corpora import report

HEADER = [
    "phase", "corpus", "model", "question_id", "depth", "status", "citation_overlap", "citation_extra",
    "entity_overlap", "latency_seconds", "judge_verdict", "judge_completeness",
]  # fmt: skip


def _run_dir(tmp_path: Path, rows: list[list], *, ledger: dict | None = None, state: dict | None = None) -> Path:
    run = tmp_path / "results" / "Bench 01"
    run.mkdir(parents=True)
    with (run / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([HEADER, *rows])
    if ledger is not None:
        (run / "spend.json").write_text(json.dumps(ledger))
    if state is not None:
        (run / "state.json").write_text(json.dumps(state))
    return run


LEDGER = {
    "run": {
        "run_id": "bench-01",
        "judge_model": "claude-sonnet-4-6",
        "suite_commit": "abc1234",
        "started_at": "2026-09-18T20:00:00Z",
        "host": "ix",
    },
    "cap_usd": 25.0,
    "total_usd": 3.5,
    "calls": 120,
    "estimated_calls": 0,
    "prices_verified": "2026-09-18",
    "by_kind": {
        "judge": {
            "calls": 100,
            "units": 100,
            "input_tokens": 300000,
            "cache_tokens": 0,
            "output_tokens": 50000,
            "usd": 1.65,
        },
        # A cached tool loop: the prompt went through the cache and `input_tokens` is a few tokens a call.
        "baseline_question": {
            "calls": 20,
            "units": 4,
            "input_tokens": 12,
            "cache_tokens": 900000,
            "cache_write_tokens": 200000,
            "cache_read_tokens": 700000,
            "output_tokens": 8000,
            "usd": 1.85,
        },
    },
}
ROWS = [
    [4, "cuad", "qwen3-8b", "q1", "standard", "done", "0.500", 1, "0.400", "120.0", "pass", 4],
    [4, "cuad", "qwen3-8b", "q2", "standard", "done", "0.700", 0, "0.600", "80.0", "fail", 2],
    [4, "cuad", "qwen3-8b", "q3", "standard", "degraded", "0.000", 0, "0.000", "2.0", "", 0],
    [4, "cuad", "qwen35-9b", "q1", "standard", "done", "0.900", 0, "0.800", "60.0", "", 0],
    [5, "enron", "qwen36-35b-a3b", "q1", "standard", "error", "0.000", 0, "0.000", "0.0", "", 0],
]
FIT = {"verdict": "pass", "checked_at": "2026-09-19T01:00:00Z", "pinned_llama_cpp": "v0.4.1", "checks": []}


def _render(run: Path, preflight=FIT, commit="abc1234") -> str:
    # Rendered on another machine, a day later: the run's own host and commit are what the report cites.
    return report.render(run, preflight=preflight, today="2026-09-19", host="laptop", commit=commit)


def test_the_header_names_run_commit_corpora_judge_spend_and_the_preflight(tmp_path):
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    assert text.startswith("# Benchmark run `bench-01` on ix, 2026-09-19\n")
    assert "- Suite commit: `abc1234`\n- Run started: 2026-09-18T20:00:00Z\n" in text
    assert "- Corpora: cuad, enron\n" in text
    assert (
        "Cloud spend: USD 3.50 of a 25.00 cap over 120 calls; judge claude-sonnet-4-6; prices as of 2026-09-18." in text
    )
    assert "- Machine preflight: **pass** at 2026-09-19T01:00:00Z, llama.cpp pin v0.4.1.\n" in text
    assert "not a baseline" not in text


def test_the_commit_is_the_one_that_ran_the_sweep_not_the_one_the_report_was_rendered_at(tmp_path):
    """Found in review. A run takes a day. A pull in between, and the report cited the wrong code."""
    run = _run_dir(tmp_path, ROWS, ledger=LEDGER)
    later = _render(run, commit="fff9999")
    assert "- Suite commit: `abc1234` (the commit that ran the sweep; this report was rendered at `fff9999`)\n" in later
    resumed = {**LEDGER, "run": {**LEDGER["run"], "resumed_at_commits": ["bbb2222"]}}
    assert "**Resumed at other commits: bbb2222**" in _render(_run_dir(tmp_path / "r", ROWS, ledger=resumed))
    dirty = {**LEDGER, "run": {**LEDGER["run"], "suite_commit": "abc1234-dirty"}}
    assert "**uncommitted changes**" in _render(_run_dir(tmp_path / "d", ROWS, ledger=dirty))
    unrecorded = {**LEDGER, "run": {"run_id": "bench-01"}}
    text = _render(_run_dir(tmp_path / "u", ROWS, ledger=unrecorded), commit="fff9999")
    assert "`fff9999` (the checkout this report was rendered from; the run did not record its own)" in text
    assert "- Run started: not recorded\n" in text


def test_each_model_gets_a_row_of_what_was_measured(tmp_path):
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    assert (
        "| cuad | `qwen3-8b` | n/a | 3 | 2 | 1 | 0 | 0 | 0 | 0.60 | 0.50 | 100 | 2 | 1 | 0 | 1 | n/a | 3.00 | n/a |"
        in text
    )
    # Finished and never judged: absent from the judged columns, not scored zero.
    assert (
        "| cuad | `qwen35-9b` | n/a | 1 | 1 | 0 | 0 | 0 | 0 | 0.90 | 0.80 | 60 | 0 | 0 | 0 | 0 | n/a | n/a | n/a |"
        in text
    )
    assert (
        "| enron | `qwen36-35b-a3b` | n/a | 1 | 0 | 0 | 0 | 0 | 1 | n/a | n/a | n/a | 0 | 0 | 0 | 0 | n/a | n/a | n/a |"
        in text
    )
    assert "## Phase 4: every model, every question" in text and "## Phase 5: the two largest, judged" in text
    assert "1 finished unit(s) in the judged phases have no verdict (an unusable baseline, a judge failure" in text
    assert "The overlap means include units whose baseline was unusable" in text


def _planned(model: str, corpus: str, n: int, *, phase: int = 4, status: str = "pending", error=None) -> list[dict]:
    return [
        {"phase": phase, "corpus": corpus, "model": model, "question_id": f"q{i}", "status": status, "error": error}
        for i in range(n)
    ]


def test_units_that_never_ran_are_not_hidden_behind_the_ones_that_did(tmp_path):
    """Found in review. The report read state.json only for skipped models, so a model with 3 of 16 units
    run (the spend cap, the circuit breaker) showed `units 3 | done 2` and nothing else. This is the
    document a model gets retired on."""
    state = {
        "units": [
            *_planned("qwen3-8b", "cuad", 16),
            *_planned("qwen35-9b", "cuad", 16),
            *_planned("qwen3-4b", "cuad", 16),  # planned, and not one unit ran
            *_planned("qwen36-35b-a3b", "enron", 4, phase=5),
            *_planned("gemma4-26b-a4b", "cuad", 16, status="skipped", error="skipped before the run: not downloaded"),
        ]
    }
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER, state=state))
    assert "| cuad | `qwen3-8b` | 16 | 3 | 2 | 1 | 0 | 0 | 0 |" in text
    assert (
        "| cuad | `qwen3-4b` | 16 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | 0 | 0 | 0 | 0 | n/a | n/a | n/a |" in text
    )
    assert "| enron | `qwen36-35b-a3b` | 4 | 1 | 0 | 0 | 0 | 0 | 1 |" in text
    assert "gemma4-26b-a4b` |" not in text.split("## Models this machine did not run")[0], "it has its own section"
    assert "**47 planned unit(s) never ran**" in text  # 52 planned, 5 ran


def test_corpora_are_not_pooled_into_one_row(tmp_path):
    rows = [
        [4, "cuad", "qwen3-8b", "q1", "standard", "done", "1.000", 0, "1.000", "10.0", "pass", 5],
        [4, "enron", "qwen3-8b", "q1", "standard", "done", "0.000", 0, "0.000", "300.0", "fail", 0],
    ]
    text = _render(_run_dir(tmp_path, rows, ledger=LEDGER))
    assert (
        "| cuad | `qwen3-8b` | n/a | 1 | 1 | 0 | 0 | 0 | 0 | 1.00 | 1.00 | 10 | 1 | 1 | 0 | 0 | n/a | 5.00 | n/a |"
        in text
    )
    assert (
        "| enron | `qwen3-8b` | n/a | 1 | 1 | 0 | 0 | 0 | 0 | 0.00 | 0.00 | 300 | 1 | 0 | 0 | 1 | n/a | 0.00 | n/a |"
        in text
    )


def test_the_host_is_the_machine_that_ran_the_sweep(tmp_path):
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    assert text.startswith("# Benchmark run `bench-01` on ix, 2026-09-19\n")
    no_host = {**LEDGER, "run": {k: v for k, v in LEDGER["run"].items() if k != "host"}}
    from_preflight = _render(_run_dir(tmp_path / "p", ROWS, ledger=no_host), preflight={**FIT, "host": "Mini.local"})
    assert from_preflight.startswith("# Benchmark run `bench-01` on mini, ")
    assert _render(_run_dir(tmp_path / "n", ROWS, ledger=no_host)).startswith("# Benchmark run `bench-01` on laptop, ")


def test_a_rerun_unit_counts_once_by_its_last_row(tmp_path):
    """metrics.csv is append-only; --rerun appends a second row for the same unit."""
    again = [*ROWS, [4, "cuad", "qwen3-8b", "q3", "standard", "done", "0.600", 0, "0.500", "90.0", "pass", 5]]
    text = _render(_run_dir(tmp_path, again, ledger=LEDGER))
    assert (
        "| cuad | `qwen3-8b` | n/a | 3 | 3 | 0 | 0 | 0 | 0 | 0.60 | 0.50 | 90 | 3 | 2 | 0 | 1 | n/a | 3.67 | n/a |"
        in text
    )


def test_a_failed_or_missing_preflight_says_the_timings_are_not_a_baseline(tmp_path):
    run = _run_dir(tmp_path, ROWS, ledger=LEDGER)
    throttled = {
        **FIT,
        "verdict": "fail",
        "checks": [
            {"name": "thermal pressure", "status": "fail", "detail": "Heavy (level 2)"},
            {"name": "GPU at idle", "status": "warn", "detail": "45% busy"},
            {"name": "power source", "status": "pass", "detail": "AC"},
        ],
    }
    text = _render(run, preflight=throttled)
    assert "**fail**" in text and "**The timings below are not a baseline.**" in text
    assert "  - fail: thermal pressure: Heavy (level 2)\n  - warn: GPU at idle: 45% busy\n" in text
    assert "power source" not in text, "a passing check is not news"
    assert "Machine preflight: **not recorded.**" in _render(run, preflight=None)
    assert "not a baseline" in _render(run, preflight=None)


def test_what_is_missing_is_said_not_left_blank(tmp_path):
    bare = _render(_run_dir(tmp_path, []))
    assert "**no spend.json in the run directory**" in bare and "No metrics.csv rows" in bare
    assert "**uncommitted changes**" in _render(_run_dir(tmp_path / "b", ROWS), commit="abc1234-dirty")
    assert "The overlap means" not in bare


def test_models_the_machine_did_not_run_are_listed_with_the_reason(tmp_path):
    state = {
        "units": [
            {
                "phase": 4,
                "corpus": "cuad",
                "model": "qwen35-9b",
                "status": "skipped",
                "error": "skipped before the run: not downloaded on this instance",
            },
            {"phase": 4, "corpus": "cuad", "model": "qwen3-4b", "status": "skipped", "error": None},
            {"phase": 4, "corpus": "cuad", "model": "qwen3-8b", "status": "done", "error": None},
        ]
    }
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER, state=state))
    assert "## Models this machine did not run\n\n- `qwen35-9b`: not downloaded on this instance\n" in text
    assert "`qwen3-4b`" not in text.split("## Models this machine did not run")[1].split("##")[0]


def test_spend_is_broken_down_by_call_kind_and_the_reading_is_left_to_the_runner(tmp_path):
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    assert "| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |" in text
    assert "| judge | 100 | 100 | 300000 | 0 | 0 | 50000 | 1.6500 |" in text, "a ledger row without the cache columns"
    assert "| baseline_question | 4 | 20 | 12 | 200000 | 700000 | 8000 | 1.8500 |" in text, (
        "the cache columns are where a cached prompt went; 12 input tokens alone would misstate the run"
    )
    assert text.rstrip().endswith("what was not verified._")


def test_one_file_per_run_never_overwritten(tmp_path, monkeypatch):
    run = _run_dir(tmp_path, ROWS, ledger=LEDGER)
    (run / "preflight.json").write_text(json.dumps(FIT))
    monkeypatch.setattr(report.platform, "node", lambda: "Laptop.local")  # not where it ran: the ledger says ix
    monkeypatch.setattr(report.time, "strftime", lambda fmt: "2026-09-19")
    monkeypatch.setattr(report, "suite_commit", lambda: "abc1234")
    out = tmp_path / "reports"
    first = report.write(run, out, preflight_path=None)
    second = report.write(run, out, preflight_path=None)
    assert (
        first.name == "2026-09-19-benchmark-ix-bench-01.md" and second.name == "2026-09-19-benchmark-ix-bench-01-2.md"
    )
    assert "**pass**" in first.read_text(), "the run directory's own preflight.json is picked up"
    assert report.main(["--run-dir", str(tmp_path / "absent"), "--out", str(out)]) == 2


def test_a_check_that_could_not_look_is_in_the_report(tmp_path):
    blind = {
        **FIT,
        "verdict": "warn",
        "checks": [{"name": "thermal pressure", "status": "skipped", "detail": "could not read the level"}],
    }
    text = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER), preflight=blind)
    assert "**warn**" in text and "  - skipped: thermal pressure: could not read the level\n" in text


def test_every_phase_that_runs_a_model_is_in_the_report_and_in_the_count(tmp_path):
    """Found in review. The default --phases includes 6. Its rows got no table, its planned units were left
    out of "never ran", and `unified` appeared under Corpora with nothing shown for it."""
    rows = [*ROWS, [6, "unified", "qwen36-35b-a3b", "q1", "standard", "done", "0.400", 0, "0.300", "500.0", "", 0]]
    state = {
        "units": [
            *_planned("qwen36-35b-a3b", "unified", 9, phase=6),
            *_planned("qwen36-35b-a3b", "cuad", 1, phase=2),
            *_planned("qwen3-8b", "synthetic", 16, phase=4),  # planned, and the run stopped before it
            *_planned("-", "cuad", 1, phase=0),
        ]
    }
    text = _render(_run_dir(tmp_path, rows, ledger=LEDGER, state=state))
    assert "## Phase 6: the unified corpus" in text
    assert "| unified | `qwen36-35b-a3b` | 9 | 1 | 1 | 0 | 0 | 0 | 0 | 0.40 | 0.30 | 500 |" in text
    assert "## Phase 2: smoke" in text and "| cuad | `qwen36-35b-a3b` | 1 | 0 |" in text
    assert "- Corpora: cuad, enron, synthetic, unified\n" in text, "synthetic was planned and produced nothing"
    assert "| synthetic | `qwen3-8b` | 16 | 0 |" in text
    assert "`-`" not in text, "phase 0 acquires a corpus; it runs no model"


def test_an_untracked_file_does_not_make_the_suite_dirty(monkeypatch):
    """An earlier render leaves a report untracked in docs/reports/. That is not a change to the suite."""
    asked = []

    def run(argv, **kw):
        asked.append(argv)
        return type("Done", (), {"stdout": "abc1234\n" if "rev-parse" in argv else ""})()

    monkeypatch.setattr(report.subprocess, "run", run)
    assert report.suite_commit() == "abc1234"
    assert "--untracked-files=no" in asked[1]
    monkeypatch.setattr(report.subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert report.suite_commit() == "unknown"


def test_a_baseline_stopped_at_the_call_limit_is_named_in_the_report(tmp_path):
    """Every model is scored against the baseline. One that was made to answer with its tools off (#682) is a
    worse reference, and a table cannot show that."""
    run = _run_dir(tmp_path, ROWS, ledger=LEDGER)
    assert "model-call limit" not in _render(run)
    folder = run / "baselines" / "cuad"
    folder.mkdir(parents=True)
    (folder / "cuad-research-6.json").write_text(json.dumps({"answer": "a", "stopped_by": "model_call_limit"}))
    (folder / "cuad-ask-1.json").write_text(json.dumps({"answer": "a", "stopped_by": "end_turn"}))
    (folder / "cuad-ask-2.json").write_text(json.dumps({"answer": "from before the field existed"}))
    text = _render(run)
    assert "**1 baseline(s) were stopped at the model-call limit**" in text and "`cuad/cuad-research-6`" in text
    assert "cuad-ask-1" not in text.split("model-call limit")[1].split("\n")[0]


def test_a_run_with_the_answers_question_column_reports_it(tmp_path):
    """Rows from before the column show n/a there, never a zero that reads as a score (the test above)."""
    header = [*HEADER, "judge_answers_question"]
    rows = [[*r, ""] for r in ROWS]
    for r in rows:
        if r[HEADER.index("judge_verdict")]:
            r[-1] = "4"
    run = tmp_path / "results" / "Bench 02"
    run.mkdir(parents=True)
    with (run / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([header, *rows])
    (run / "spend.json").write_text(json.dumps(LEDGER))
    text = _render(run)
    assert "| answers question (0-5) | completeness (0-5) | answer key (0-1) |" in text
    assert (
        "| cuad | `qwen3-8b` | n/a | 3 | 2 | 1 | 0 | 0 | 0 | 0.60 | 0.50 | 100 | 2 | 1 | 0 | 1 | 4.00 | 3.00 | n/a |"
        in text
    )


def test_the_answer_key_column_is_averaged_over_the_rows_that_have_one(tmp_path):
    """A run on the standard questions has no key: n/a, not 0. A keyed run averages the answers that were keyed."""
    plain = _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    assert "| answer key (0-1) |" in plain
    assert plain.count("| n/a |\n") >= 1 and "| 2 | 1 | 0 | 1 | n/a | 3.00 | n/a |" in plain
    header = [*HEADER, "answer_key_score"]
    rows = [[*r, ""] for r in ROWS]
    keyed = [
        r
        for r in rows
        if r[HEADER.index("model")] == "qwen3-8b"
        and r[HEADER.index("corpus")] == "cuad"
        and r[HEADER.index("status")] == "done"
    ]
    keyed[0][-1], keyed[1][-1] = "1.000", "0.500"
    run = tmp_path / "results" / "Bench 03"
    run.mkdir(parents=True)
    with (run / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([header, *rows])
    (run / "spend.json").write_text(json.dumps(LEDGER))
    assert "| 2 | 1 | 0 | 1 | n/a | 3.00 | 0.75 |" in _render(run)


def test_the_report_says_when_units_ran_beside_a_summarize_backlog():
    """A row's summarize_backlog is what the unit started beside; 0 on every row says nothing, anything else is
    said once, after the model tables and before the spend, with the largest backlog seen."""
    from scripts.test_corpora.report import _summaries_note

    quiet = [{"phase": "4", "summarize_backlog": "0"}, {"phase": "4", "summarize_backlog": "0"}]
    assert _summaries_note(quiet) == []
    predates = [{"phase": "4"}, {"phase": "1", "summarize_backlog": "9"}]
    assert _summaries_note(predates) == [], "no model unit carries a backlog: the phase-1 baseline does not count"
    beside = [
        {"phase": "4", "summarize_backlog": "5745"},
        {"phase": "4", "summarize_backlog": "0"},
        {"phase": "4", "summarize_backlog": "12"},
    ]
    assert _summaries_note(beside)[0].startswith("**2 of 3 units ran while summaries were still queued** (up to 5,745")


def test_a_rendered_report_carries_the_summaries_note_when_a_unit_ran_beside_a_backlog(tmp_path):
    header = [*HEADER, "summarize_backlog"]
    rows = [[*r, "0"] for r in ROWS]
    rows[0][-1] = "5745"
    run = tmp_path / "results" / "Bench 04"
    run.mkdir(parents=True)
    with (run / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([header, *rows])
    (run / "spend.json").write_text(json.dumps(LEDGER))
    text = _render(run)
    assert "ran while summaries were still queued** (up to 5,745" in text
    quiet = tmp_path / "results" / "Bench 05"
    quiet.mkdir(parents=True)
    with (quiet / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([header, *[[*r, "0"] for r in ROWS]])
    (quiet / "spend.json").write_text(json.dumps(LEDGER))
    assert "summaries were still queued" not in _render(quiet)


def test_forced_answers_are_counted_and_judged_beside_the_chosen_ones(tmp_path):
    """#719: an answer the app forced is degraded and judged. The table counts it under `forced` and `forced
    pass`, never under `judged`; the note says what the columns mean. A file from before the column is read
    as before."""
    header = [*HEADER, "stop_reason"]
    rows = [
        ["4", "cuad", "m", "q1", "standard", "done", "0.5", "0", "0.5", "10", "pass", "4", ""],
        ["4", "cuad", "m", "q2", "standard", "degraded", "0.0", "0", "0.0", "310", "pass", "3", "time_budget"],
        ["4", "cuad", "m", "q3", "standard", "degraded", "0.0", "0", "0.0", "320", "fail", "1", "time_budget"],
        ["4", "cuad", "m", "q4", "standard", "degraded", "0.0", "0", "0.0", "2", "", "", ""],  # an empty answer
    ]
    run = tmp_path / "results" / "r"
    run.mkdir(parents=True)
    with (run / "metrics.csv").open("w", newline="") as f:
        csv.writer(f).writerows([header, *rows])
    text = report.render(run, preflight=None, today="2026-09-26", host="ix", commit="abc1234")
    row = next(line for line in text.splitlines() if line.startswith("| cuad | `m` |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    columns = [
        c.strip()
        for c in next(line for line in text.splitlines() if line.startswith("| corpus |")).strip("|").split("|")
    ]
    by = dict(zip(columns, cells))
    assert by["done"] == "1" and by["degraded"] == "3"
    assert by["forced"] == "2" and by["forced pass"] == "1"
    assert by["judged"] == "1" and by["pass"] == "1", "the chosen answers only"
    assert "2 answers were forced by the app" in text and "2 by time budget" in text

    # Before the column existed: nothing forced, no note.
    old = _run_dir(tmp_path, [["4", "cuad", "m", "q1", "standard", "degraded", "0", "0", "0", "3", "", ""]])
    text = report.render(old, preflight=None, today="2026-09-26", host="ix", commit="abc1234")
    assert "forced by the app" not in text
    row = next(line for line in text.splitlines() if line.startswith("| cuad | `m` |"))
    assert "| 0 | 0 |" in row  # forced, forced pass


def _subject_run(tmp_path: Path, *, model: str = "claude-fable-5-1", calls: bool = True, questions_dir=None) -> Path:
    """A keyed run: three keyed baselines (two governing-law facts, one date), one unkeyed research baseline, and
    the key file where the ledger says the questions came from."""
    keys = tmp_path / "keyed"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "cuad.key.json").write_text(
        json.dumps(
            {
                "cuad-key-1": {"kind": "fact", "all_of": [["California"]]},
                "cuad-key-2": {"kind": "fact", "all_of": [["New York"]]},
                "cuad-key-3": {"kind": "date", "date": "2022-03-15"},
                "cuad-key-4": {"kind": "list", "positives": ["A_x"], "universe": ["A_x", "B_y"]},
            }
        )
    )
    ledger = {
        **LEDGER,
        "run": {
            **LEDGER["run"],
            "baseline_model": model,
            "questions_dir": str(keys if questions_dir is None else questions_dir),
        },
        "by_model": {model: {"calls": 7, "usd": 1.2345}},
    }
    run = _run_dir(tmp_path, ROWS, ledger=ledger)
    folder = run / "baselines" / "cuad"
    folder.mkdir(parents=True)
    answers = {
        "cuad-key-1": ("California law governs.", "end_turn", 2),
        "cuad-key-2": ("Delaware law governs.", "model_call_limit", 3),
        "cuad-key-3": ("Dated March 15, 2022.", "end_turn", 2),
        "cuad-research-1": ("not a keyed question", "end_turn", 5),
    }
    for qid, (answer, stopped_by, n) in answers.items():
        record = {"question_id": qid, "answer": answer, "model": model, "stopped_by": stopped_by}
        if calls:
            record["model_call_count"] = n
        (folder / f"{qid}.json").write_text(json.dumps(record))
    return run


def test_the_cloud_subject_is_scored_against_the_answer_key_in_its_own_table(tmp_path):
    """The 2026-09-28 report scored the Claude baseline against the key by hand ("scored offline"). The report
    does it: the baselines of a keyed corpus, scored as phase 4 is, with calls, the ones stopped at the limit,
    the per-kind means and the model's spend."""
    text = _render(_subject_run(tmp_path))
    assert "## Cloud subject" in text
    assert (
        "| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | USD |"
        in text
    )
    # USD is the kind's spend (baseline_question), not the model's, which would count synthetic generation too.
    assert "| cuad | `claude-fable-5-1` | 3 | 7 | 1 | 0.67 | 0.50 | 1.00 | n/a | 1.8500 |" in text
    assert "1.2345" not in text
    assert "cuad-research-1" not in text.split("## Cloud subject")[1].split("##")[0], (
        "an unkeyed baseline is not scored"
    )
    assert text.index("## Cloud subject") < text.index("## Phase 4"), "above the phase tables"
    assert "baseline claude-fable-5-1; prices as of" in text, "the header names the subject"


def test_a_cloud_subject_run_from_before_the_call_count_takes_its_calls_from_the_ledger(tmp_path):
    """The ledger counts the whole run's baseline calls; they are this row's only when every baseline is keyed."""
    run = _subject_run(tmp_path, model="claude-sonnet-4-6", calls=False)
    assert "| cuad | `claude-sonnet-4-6` | 3 | n/a | 1 | 0.67 | 0.50 | 1.00 | n/a | 1.8500 |" in _render(run), (
        "an unkeyed research baseline is among the 20 calls"
    )
    (run / "baselines" / "cuad" / "cuad-research-1.json").unlink()
    assert "| cuad | `claude-sonnet-4-6` | 3 | 20 | 1 | 0.67 | 0.50 | 1.00 | n/a | 1.8500 |" in _render(run)


def test_a_run_without_a_key_has_no_cloud_subject_table(tmp_path):
    run = _run_dir(tmp_path, ROWS, ledger=LEDGER)
    folder = run / "baselines" / "cuad"
    folder.mkdir(parents=True)
    (folder / "cuad-research-1.json").write_text(json.dumps({"answer": "a", "model": "claude-sonnet-4-6"}))
    assert "Cloud subject" not in _render(run)
    # A keyed questions directory that is not where the ledger says: no table rather than a wrong one.
    assert "Cloud subject" not in _render(_subject_run(tmp_path / "elsewhere", questions_dir=tmp_path / "absent"))


def test_a_kind_the_key_declares_beyond_the_three_gets_a_column_and_one_the_scorer_refuses_is_said(
    tmp_path, monkeypatch
):
    """The key file may declare kinds beyond fact, date and list (the synthetic corpus's key is being written):
    each gets a column. A kind this checkout's scorer does not know is left out and named, not a crash."""
    from scripts.test_corpora.runner import answer_key

    run = _subject_run(tmp_path)
    key_path = Path(json.loads((run / "spend.json").read_text())["run"]["questions_dir"]) / "cuad.key.json"
    key = json.loads(key_path.read_text())
    key["cuad-key-5"] = {"kind": "number", "value": 42}
    key["cuad-key-6"] = {"kind": "colour", "value": "red"}
    key_path.write_text(json.dumps(key))
    for qid in ("cuad-key-5", "cuad-key-6"):
        (run / "baselines" / "cuad" / f"{qid}.json").write_text(
            json.dumps({"answer": "42", "model": "claude-fable-5-1", "model_call_count": 1})
        )
    original = answer_key.score

    def score(entry, answer):
        return {"score": 1.0} if entry["kind"] == "number" else original(entry, answer)

    monkeypatch.setattr(report.answer_key, "score", score)
    text = _render(run)
    assert "| answer key (0-1) | fact | date | list | number | USD |" in text
    assert "| cuad | `claude-fable-5-1` | 4 | 8 | 1 | 0.75 | 0.50 | 1.00 | n/a | 1.00 | 1.8500 |" in text
    assert (
        "1 keyed baseline(s) have a kind this scorer does not know and are not in the table: `cuad/cuad-key-6` (colour)"
        in text
    )


def test_cache_writes_are_shown_by_lifetime_when_the_ledger_has_them(tmp_path):
    """What corrects spend.yaml's estimate: the writes as the meter priced them. A ledger from before the split
    says nothing."""
    assert "by lifetime" not in _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    split = json.loads(json.dumps(LEDGER))
    split["by_kind"]["baseline_question"].update({"cache_write_5m_tokens": 200000, "cache_write_1h_tokens": 0})
    split["by_kind"]["judge"].update({"cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0})
    text = _render(_run_dir(tmp_path / "s", ROWS, ledger=split))
    assert "Cache writes by lifetime, as priced: baseline_question 200,000 at five minutes, 0 at an hour.\n" in text
    assert "judge 0 at" not in text, "a kind that wrote nothing has nothing to break down"


def test_thinking_is_shown_among_the_output_tokens_when_the_ledger_has_it(tmp_path):
    """What corrects spend.yaml's provisional thinking_output_tokens. Nothing for a run that recorded none."""
    assert "Thinking among" not in _render(_run_dir(tmp_path, ROWS, ledger=LEDGER))
    thought = json.loads(json.dumps(LEDGER))
    thought["by_kind"]["baseline_question"]["thinking_tokens"] = 6000
    thought["by_kind"]["judge"]["thinking_tokens"] = 0
    text = _render(_run_dir(tmp_path / "t", ROWS, ledger=thought))
    assert "Thinking among the output tokens: baseline_question 6,000 of 8,000.\n" in text
