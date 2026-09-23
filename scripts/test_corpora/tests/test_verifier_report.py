from __future__ import annotations

import csv
import json

import pytest

from scripts.test_corpora.runner.verifier_report import (
    ReportThresholds,
    build_report,
    main,
    read_verifier_rows,
    summarize_rows,
)


def _write_metrics(path, rows, *, include_verifier=True):
    fieldnames = [
        "phase",
        "corpus",
        "model",
        "question_id",
        "depth",
        "status",
        "citation_overlap",
        "citation_extra",
        "entity_overlap",
        "latency_seconds",
        "judge_verdict",
        "judge_completeness",
    ]
    if include_verifier:
        fieldnames.extend(
            [
                "verifier_total",
                "verifier_supported",
                "verifier_partial",
                "verifier_unsupported",
                "verifier_skipped",
            ]
        )
    else:
        rows = [{key: value for key, value in row.items() if key in fieldnames} for row in rows]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _row(
    *,
    corpus="cuad",
    model="qwen36-35b-a3b",
    question_id="q1",
    total=0,
    supported=0,
    partial=0,
    unsupported=0,
    skipped=0,
):
    return {
        "phase": "4",
        "corpus": corpus,
        "model": model,
        "question_id": question_id,
        "depth": "standard",
        "status": "done",
        "citation_overlap": "0.5",
        "citation_extra": "0",
        "entity_overlap": "0.5",
        "latency_seconds": "12.0",
        "judge_verdict": "",
        "judge_completeness": "",
        "verifier_total": str(total),
        "verifier_supported": str(supported),
        "verifier_partial": str(partial),
        "verifier_unsupported": str(unsupported),
        "verifier_skipped": str(skipped),
    }


def test_read_verifier_rows_detects_legacy_metrics(tmp_path):
    metrics = tmp_path / "metrics.csv"
    _write_metrics(metrics, [_row()], include_verifier=False)

    rows, has_verifier_columns = read_verifier_rows(metrics)

    assert rows == []
    assert has_verifier_columns is False


def test_summarize_rows_candidate_when_supported_verdicts_dominate(tmp_path):
    metrics = tmp_path / "metrics.csv"
    _write_metrics(
        metrics,
        [
            _row(question_id="q1", total=10, supported=10),
            _row(question_id="q2", total=10, supported=10),
        ],
    )
    rows, has_verifier_columns = read_verifier_rows(metrics)

    summary = summarize_rows(rows, "overall", ReportThresholds(min_verdicts=20))

    assert has_verifier_columns is True
    assert summary.total == 20
    assert summary.supported == 20
    assert summary.hint == "candidate"
    assert "spot-check" in summary.reason


def test_summarize_rows_insufficient_data_below_threshold(tmp_path):
    metrics = tmp_path / "metrics.csv"
    _write_metrics(metrics, [_row(total=5, supported=5)])
    rows, _ = read_verifier_rows(metrics)

    summary = summarize_rows(rows, "overall", ReportThresholds(min_verdicts=20))

    assert summary.hint == "insufficient-data"
    assert "below the 20 minimum" in summary.reason


def test_summarize_rows_review_required_for_unsupported_verdicts(tmp_path):
    metrics = tmp_path / "metrics.csv"
    _write_metrics(
        metrics,
        [
            _row(question_id="q1", total=10, supported=10),
            _row(question_id="q2", total=10, supported=9, unsupported=1),
        ],
    )
    rows, _ = read_verifier_rows(metrics)

    summary = summarize_rows(rows, "overall", ReportThresholds(min_verdicts=20))

    assert summary.hint == "review-required"
    assert "unsupported rate" in summary.reason


def test_build_report_groups_by_corpus_and_model(tmp_path):
    run_dir = tmp_path / "results" / "verifier-smoke"
    run_dir.mkdir(parents=True)
    _write_metrics(
        run_dir / "metrics.csv",
        [
            _row(corpus="cuad", model="qwen36-35b-a3b", question_id="q1", total=20, supported=20),
            _row(corpus="enron", model="gemma4-26b-a4b", question_id="q2", total=20, supported=15, partial=5),
        ],
    )

    markdown, payload = build_report(
        run_dir / "metrics.csv",
        run_label="verifier-smoke",
        thresholds=ReportThresholds(min_verdicts=20),
    )

    assert "# Verifier Validation Report - verifier-smoke" in markdown
    assert "| cuad | candidate |" in markdown
    assert "| enron | review-required |" in markdown
    assert payload["overall"]["verifier_total"] == 40
    assert {row["label"] for row in payload["by_model"]} == {"gemma4-26b-a4b", "qwen36-35b-a3b"}


def test_build_report_missing_verifier_columns_is_insufficient(tmp_path):
    run_dir = tmp_path / "results" / "legacy-run"
    run_dir.mkdir(parents=True)
    _write_metrics(run_dir / "metrics.csv", [_row()], include_verifier=False)

    markdown, payload = build_report(
        run_dir / "metrics.csv",
        run_label="legacy-run",
        thresholds=ReportThresholds(),
    )

    assert "has no verifier columns" in markdown
    assert payload["hint"] == "insufficient-data"
    assert payload["overall"] is None


def test_cli_writes_markdown_and_json_outputs(tmp_path):
    run_dir = tmp_path / "results" / "verifier-smoke"
    run_dir.mkdir(parents=True)
    _write_metrics(run_dir / "metrics.csv", [_row(total=20, supported=20)])
    report_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"

    rc = main(
        [
            "--run-dir",
            str(run_dir),
            "--output",
            str(report_path),
            "--json-output",
            str(json_path),
            "--min-verdicts",
            "20",
        ]
    )

    assert rc == 0
    assert "Verifier Validation Report" in report_path.read_text()
    payload = json.loads(json_path.read_text())
    assert payload["hint"] == "candidate"


def test_a_metrics_row_takes_the_shape_of_the_files_own_header():
    """New columns go at the end, and a resumed run keeps writing the columns its file started with."""
    from scripts.test_corpora.runner.sweep import METRICS_COLUMNS, metrics_row_for

    assert METRICS_COLUMNS[-2:] == ("judge_answers_question", "answer_key_score") and METRICS_COLUMNS[:12] == (
        "phase", "corpus", "model", "question_id", "depth", "status", "citation_overlap", "citation_extra",
        "entity_overlap", "latency_seconds", "judge_verdict", "judge_completeness",
    )  # fmt: skip
    values = {c: f"<{c}>" for c in METRICS_COLUMNS}
    assert metrics_row_for(list(METRICS_COLUMNS), **values) == [f"<{c}>" for c in METRICS_COLUMNS]
    older = list(METRICS_COLUMNS[:-1])
    assert metrics_row_for(older, **values) == [f"<{c}>" for c in older], "an older file: its columns, its order"
    oldest = list(METRICS_COLUMNS[:12])
    assert len(metrics_row_for(oldest, **values)) == 12
    import pytest

    with pytest.raises(KeyError):
        metrics_row_for(["phase", "renamed_column"], **values)


def test_a_metrics_file_with_a_column_this_sweep_cannot_write_is_refused_at_startup():
    """Review of #695: the row builder would have raised at the first unit's row, after that unit was marked done,
    losing its row to --resume. The header is read at startup; that is where the refusal belongs."""
    from scripts.test_corpora.runner.sweep import METRICS_COLUMNS, unknown_metrics_columns

    unknown_metrics_columns(list(METRICS_COLUMNS))
    unknown_metrics_columns(list(METRICS_COLUMNS[:12]))  # an older file: fine, its columns are all known
    with pytest.raises(SystemExit, match="a_column_from_the_future"):
        unknown_metrics_columns([*METRICS_COLUMNS, "a_column_from_the_future"])
