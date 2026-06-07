"""Summarize citation-verifier validation metrics from a sweep run.

The verifier is a candidate display-only "citation support" signal, not a
calibrated correctness score. This reporter turns the raw ``metrics.csv``
verdict columns into a small release-decision artifact: enough data, observed
verdict mix, and whether the signal is quiet enough to consider for UI work or
needs manual review first.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

VERIFIER_COLUMNS = (
    "verifier_total",
    "verifier_supported",
    "verifier_partial",
    "verifier_unsupported",
    "verifier_skipped",
)


@dataclasses.dataclass(frozen=True)
class VerifierRow:
    phase: str
    corpus: str
    model: str
    question_id: str
    depth: str
    status: str
    total: int
    supported: int
    partial: int
    unsupported: int
    skipped: int


@dataclasses.dataclass(frozen=True)
class VerifierSummary:
    label: str
    rows: int
    rows_with_verdicts: int
    total: int
    supported: int
    partial: int
    unsupported: int
    skipped: int
    hint: str
    reason: str

    @property
    def supported_rate(self) -> float:
        return _rate(self.supported, self.total)

    @property
    def partial_rate(self) -> float:
        return _rate(self.partial, self.total)

    @property
    def unsupported_rate(self) -> float:
        return _rate(self.unsupported, self.total)

    @property
    def skipped_rate(self) -> float:
        return _rate(self.skipped, self.total)


@dataclasses.dataclass(frozen=True)
class ReportThresholds:
    min_verdicts: int = 20
    max_partial_rate: float = 0.10
    max_unsupported_rate: float = 0.00
    max_skipped_rate: float = 0.25


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _int_value(row: dict[str, str], key: str) -> int:
    raw = row.get(key, "")
    if raw in ("", None):
        return 0
    return int(raw)


def read_verifier_rows(metrics_path: Path) -> tuple[list[VerifierRow], bool]:
    """Read sweep ``metrics.csv`` rows.

    Returns ``(rows, has_verifier_columns)``. Legacy resumed sweeps may preserve
    a pre-verifier CSV shape; callers should report that as insufficient data
    instead of treating it as zero verifier signal.
    """
    if not metrics_path.exists():
        raise FileNotFoundError(metrics_path)

    with metrics_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        has_verifier_columns = all(col in fieldnames for col in VERIFIER_COLUMNS)
        if not has_verifier_columns:
            return [], False

        rows = [
            VerifierRow(
                phase=row.get("phase", ""),
                corpus=row.get("corpus", ""),
                model=row.get("model", ""),
                question_id=row.get("question_id", ""),
                depth=row.get("depth", ""),
                status=row.get("status", ""),
                total=_int_value(row, "verifier_total"),
                supported=_int_value(row, "verifier_supported"),
                partial=_int_value(row, "verifier_partial"),
                unsupported=_int_value(row, "verifier_unsupported"),
                skipped=_int_value(row, "verifier_skipped"),
            )
            for row in reader
        ]
    return rows, True


def classify_summary(summary: VerifierSummary, thresholds: ReportThresholds) -> tuple[str, str]:
    """Return ``(hint, reason)`` for a summary.

    The hint is intentionally a release-planning prompt, not a product-quality
    verdict. Unsupported or partial citations may mean the verifier found real
    answer issues, or it may mean the verifier itself is noisy. Either way,
    those rows need human spot-checking before the UI becomes default-on.
    """
    if summary.total < thresholds.min_verdicts:
        return (
            "insufficient-data",
            f"{summary.total} checked citations is below the {thresholds.min_verdicts} minimum",
        )
    if summary.unsupported_rate > thresholds.max_unsupported_rate:
        return (
            "review-required",
            f"unsupported rate {summary.unsupported_rate:.1%} exceeds {thresholds.max_unsupported_rate:.1%}",
        )
    if summary.partial_rate > thresholds.max_partial_rate:
        return (
            "review-required",
            f"partial rate {summary.partial_rate:.1%} exceeds {thresholds.max_partial_rate:.1%}",
        )
    if summary.skipped_rate > thresholds.max_skipped_rate:
        return (
            "review-required",
            f"skipped rate {summary.skipped_rate:.1%} exceeds {thresholds.max_skipped_rate:.1%}",
        )
    return (
        "candidate",
        "enough checked citations and low noisy-verdict rates; spot-check before UI default-on",
    )


def summarize_rows(rows: list[VerifierRow], label: str, thresholds: ReportThresholds) -> VerifierSummary:
    base = VerifierSummary(
        label=label,
        rows=len(rows),
        rows_with_verdicts=sum(1 for row in rows if row.total > 0),
        total=sum(row.total for row in rows),
        supported=sum(row.supported for row in rows),
        partial=sum(row.partial for row in rows),
        unsupported=sum(row.unsupported for row in rows),
        skipped=sum(row.skipped for row in rows),
        hint="",
        reason="",
    )
    hint, reason = classify_summary(base, thresholds)
    return dataclasses.replace(base, hint=hint, reason=reason)


def grouped_summaries(
    rows: list[VerifierRow],
    group_by: str,
    thresholds: ReportThresholds,
) -> list[VerifierSummary]:
    buckets: dict[str, list[VerifierRow]] = defaultdict(list)
    for row in rows:
        key = getattr(row, group_by)
        buckets[key or "(blank)"].append(row)
    return [summarize_rows(bucket, label, thresholds) for label, bucket in sorted(buckets.items())]


def summary_to_dict(summary: VerifierSummary) -> dict[str, Any]:
    return {
        "label": summary.label,
        "rows": summary.rows,
        "rows_with_verdicts": summary.rows_with_verdicts,
        "verifier_total": summary.total,
        "verifier_supported": summary.supported,
        "verifier_partial": summary.partial,
        "verifier_unsupported": summary.unsupported,
        "verifier_skipped": summary.skipped,
        "supported_rate": summary.supported_rate,
        "partial_rate": summary.partial_rate,
        "unsupported_rate": summary.unsupported_rate,
        "skipped_rate": summary.skipped_rate,
        "hint": summary.hint,
        "reason": summary.reason,
    }


def render_markdown(
    *,
    run_label: str,
    overall: VerifierSummary,
    by_corpus: list[VerifierSummary],
    by_model: list[VerifierSummary],
    thresholds: ReportThresholds,
) -> str:
    lines = [
        f"# Verifier Validation Report - {run_label}",
        "",
        "This report summarizes display-only citation verifier verdicts. It is",
        "a release-planning aid for a possible citation-support or grounding-check",
        "UI, not an answer-correctness score.",
        "",
        "## Overall",
        "",
        _markdown_table([overall]),
        "",
        "## By Corpus",
        "",
        _markdown_table(by_corpus),
        "",
        "## By Model",
        "",
        _markdown_table(by_model),
        "",
        "## Thresholds",
        "",
        f"- Minimum checked citations: {thresholds.min_verdicts}",
        f"- Maximum partial rate: {thresholds.max_partial_rate:.1%}",
        f"- Maximum unsupported rate: {thresholds.max_unsupported_rate:.1%}",
        f"- Maximum skipped rate: {thresholds.max_skipped_rate:.1%}",
        "",
        "Treat `candidate` as permission to spot-check the run, not as an",
        "automatic UI-default decision. Treat `review-required` as a request to",
        "open examples and decide whether the answers are weak, the citations are",
        "thin, or the verifier is noisy.",
    ]
    return "\n".join(lines) + "\n"


def _markdown_table(summaries: list[VerifierSummary]) -> str:
    if not summaries:
        return "_No verifier rows._"
    lines = [
        "| Label | Hint | Rows | Checked | Supported | Partial | Unsupported | Skipped | Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for summary in summaries:
        lines.append(
            f"| {_escape_md(summary.label)} | {summary.hint} | {summary.rows} | {summary.total} | {summary.supported} ({summary.supported_rate:.1%}) | "
            f"{summary.partial} ({summary.partial_rate:.1%}) | {summary.unsupported} ({summary.unsupported_rate:.1%}) | "
            f"{summary.skipped} ({summary.skipped_rate:.1%}) | {_escape_md(summary.reason)} |"
        )
    return "\n".join(lines)


def _escape_md(text: str) -> str:
    return text.replace("|", "\\|")


def build_report(
    metrics_path: Path,
    *,
    run_label: str,
    thresholds: ReportThresholds,
) -> tuple[str, dict[str, Any]]:
    rows, has_verifier_columns = read_verifier_rows(metrics_path)
    if not has_verifier_columns:
        payload = {
            "run_label": run_label,
            "hint": "insufficient-data",
            "reason": "metrics.csv has no verifier columns; use a fresh verifier validation run",
            "overall": None,
            "by_corpus": [],
            "by_model": [],
        }
        text = (
            f"# Verifier Validation Report - {run_label}\n\n"
            "metrics.csv has no verifier columns. Use a fresh verifier validation run.\n"
        )
        return text, payload

    overall = summarize_rows(rows, "overall", thresholds)
    by_corpus = grouped_summaries(rows, "corpus", thresholds)
    by_model = grouped_summaries(rows, "model", thresholds)
    text = render_markdown(
        run_label=run_label,
        overall=overall,
        by_corpus=by_corpus,
        by_model=by_model,
        thresholds=thresholds,
    )
    payload = {
        "run_label": run_label,
        "hint": overall.hint,
        "reason": overall.reason,
        "overall": summary_to_dict(overall),
        "by_corpus": [summary_to_dict(item) for item in by_corpus],
        "by_model": [summary_to_dict(item) for item in by_model],
        "thresholds": dataclasses.asdict(thresholds),
    }
    return text, payload


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="verifier-report")
    p.add_argument(
        "--run-dir",
        required=True,
        help="Sweep result directory containing metrics.csv, e.g. .../results/verifier-smoke-20260607",
    )
    p.add_argument("--label", default="", help="Human-readable report label. Defaults to the run directory name.")
    p.add_argument("--output", default="", help="Write Markdown report to this path instead of stdout.")
    p.add_argument("--json-output", default="", help="Optional JSON summary output path.")
    p.add_argument("--min-verdicts", type=int, default=ReportThresholds.min_verdicts)
    p.add_argument("--max-partial-rate", type=float, default=ReportThresholds.max_partial_rate)
    p.add_argument("--max-unsupported-rate", type=float, default=ReportThresholds.max_unsupported_rate)
    p.add_argument("--max-skipped-rate", type=float, default=ReportThresholds.max_skipped_rate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    run_dir = Path(args.run_dir).expanduser()
    thresholds = ReportThresholds(
        min_verdicts=args.min_verdicts,
        max_partial_rate=args.max_partial_rate,
        max_unsupported_rate=args.max_unsupported_rate,
        max_skipped_rate=args.max_skipped_rate,
    )
    text, payload = build_report(
        run_dir / "metrics.csv",
        run_label=args.label or run_dir.name,
        thresholds=thresholds,
    )

    if args.output:
        Path(args.output).expanduser().write_text(text)
    else:
        sys.stdout.write(text)

    if args.json_output:
        Path(args.json_output).expanduser().write_text(json.dumps(payload, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
