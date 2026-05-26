"""harness audit + optional cross-judge re-score over an answer-eval run.

Reads existing captures + verdicts (from prior sweep run) and writes:
  <workdir>/answer-eval/reports/<label>/audit.json
  <workdir>/answer-eval/reports/<label>/audit.md  (Task 7)

USAGE
  audit_answer_eval.py --label <run-label> [options]

OPTIONS
  --workdir DIR                  Default: $HARBOR_CLERK_WORKDIR
                                 or ~/Library/Application Support/Harbor Clerk
  --label LABEL                  Required. Maps to reports/<label>/.
  --corpus CORPUS                synthetic | cuad | enron. Auto-resolved if only one.
  --baseline-model MODEL         Auto-resolved if only one model dir under <corpus>.
  --cross-judge MODEL            e.g. "gpt-4o". Triggers re-judge with that judge.
  --rejudge-sample N             Re-judge only N captures (random, seeded).
  --skip-static                  Skip the static audit (cross-judge only).
  --output-dir DIR               Override default report dir location.

EXIT CODES
  0 success; 1 usage/missing input; 2 partial failure (e.g. some judge_errors).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from scripts.test_corpora.runner.audit import (
    citation_hygiene,
    failure_correlation,
    tool_use_stats,
)

log = logging.getLogger("audit_answer_eval")


def default_workdir() -> Path:
    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk"


def discover_corpus_and_model(workdir: Path, *, corpus: str | None, model: str | None) -> tuple[str, str]:
    """Auto-resolve corpus and baseline-model when only one choice exists;
    otherwise exit(1) with a helpful listing."""
    captures_root = workdir / "answer-eval" / "captures"
    if not captures_root.exists():
        sys.stderr.write(f"audit: no captures dir at {captures_root}\n")
        raise SystemExit(1)

    if corpus is None:
        corpora = sorted([p.name for p in captures_root.iterdir() if p.is_dir()])
        if not corpora:
            sys.stderr.write(f"audit: no corpora found under {captures_root}\n")
            raise SystemExit(1)
        if len(corpora) > 1:
            sys.stderr.write(f"audit: multiple corpora found ({corpora}); pass --corpus\n")
            raise SystemExit(1)
        corpus = corpora[0]

    corpus_dir = captures_root / corpus
    if model is None:
        models = sorted([p.name for p in corpus_dir.iterdir() if p.is_dir()])
        if not models:
            sys.stderr.write(f"audit: no model dirs found under {corpus_dir}\n")
            raise SystemExit(1)
        if len(models) > 1:
            sys.stderr.write(f"audit: multiple model dirs found ({models}); pass --baseline-model\n")
            raise SystemExit(1)
        model = models[0]

    return corpus, model


def load_captures(workdir: Path, *, corpus: str, model: str) -> list[dict]:
    cap_dir = workdir / "answer-eval" / "captures" / corpus / model
    if not cap_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(cap_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable capture %s: %s", path.name, exc)
    return out


def load_verdicts(workdir: Path, *, label: str, corpus: str, model: str) -> list[dict]:
    """Prefer detail.json (already has id field); fall back to per-item files
    (synthesize id from filename stem)."""
    detail_path = workdir / "answer-eval" / "reports" / label / "detail.json"
    if detail_path.exists():
        try:
            return json.loads(detail_path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("detail.json unparseable: %s; falling back to per-item files", exc)

    v_dir = workdir / "answer-eval" / "verdicts" / corpus / model
    if not v_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(v_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            data["id"] = path.stem  # synthesize qid from filename
            out.append(data)
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable verdict %s: %s", path.name, exc)
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audit_answer_eval.py")
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--label", required=True)
    p.add_argument("--corpus", default=None)
    p.add_argument("--baseline-model", default=None)
    p.add_argument("--cross-judge", default=None, help="Re-judge with this judge model (e.g. gpt-4o)")
    p.add_argument("--rejudge-sample", type=int, default=None)
    p.add_argument("--skip-static", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)
    workdir = args.workdir or default_workdir()

    try:
        corpus, model = discover_corpus_and_model(workdir, corpus=args.corpus, model=args.baseline_model)
    except SystemExit as exc:
        return int(exc.code or 1)

    captures = load_captures(workdir, corpus=corpus, model=model)
    if not captures:
        sys.stderr.write(f"audit: no captures found at {workdir}/answer-eval/captures/{corpus}/{model}\n")
        return 1

    verdicts = load_verdicts(workdir, label=args.label, corpus=corpus, model=model)

    audit: dict = {
        "label": args.label,
        "corpus": corpus,
        "baseline_model": model,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "tool_use": None,
        "failure_correlation": None,
        "citation_hygiene": None,
        "cross_judge": None,
    }
    if not args.skip_static:
        audit["tool_use"] = tool_use_stats(captures)
        audit["failure_correlation"] = failure_correlation(captures, verdicts)
        audit["citation_hygiene"] = citation_hygiene(captures)

    # Cross-judge wired in Task 8.

    output_dir = args.output_dir or (workdir / "answer-eval" / "reports" / args.label)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(json.dumps(audit, indent=2, default=str))
    log.info("wrote %s", output_dir / "audit.json")
    (output_dir / "audit.md").write_text(render_markdown(audit))
    log.info("wrote %s", output_dir / "audit.md")
    return 0


def render_markdown(audit: dict) -> str:
    """Render an audit dict as a readable Markdown report."""
    out: list[str] = []
    out.append(f"# Audit — {audit['label']} ({audit['baseline_model']})")
    out.append("")
    out.append(f"Generated {audit['generated_at']}")
    out.append(f"Corpus: `{audit['corpus']}`")
    out.append("")

    tu = audit.get("tool_use")
    if tu:
        out.append("## Tool-use distribution")
        out.append("")
        out.append(f"Total captures: {tu['total_captures']}")
        out.append("")
        out.append("| tool calls | count |")
        out.append("| --- | --- |")
        for bucket, count in sorted(tu["tool_call_distribution"].items(), key=lambda kv: str(kv[0])):
            out.append(f"| {bucket} | {count} |")
        out.append("")
        out.append("| tool | calls |")
        out.append("| --- | --- |")
        for tool, count in sorted(tu["tool_call_counts_per_tool"].items(), key=lambda kv: -kv[1]):
            out.append(f"| {tool} | {count} |")
        out.append("")

    fc = audit.get("failure_correlation")
    if fc:
        out.append("## Failure correlation")
        out.append("")
        _render_fc_block(out, "Low correctness + low tool use", fc["low_correctness_low_tool_use"])
        _render_fc_block(
            out,
            "Earliest/latest questions, no kb_documents_by_date",
            fc["earliest_latest_questions_no_by_date_tool"],
        )
        _render_fc_block(
            out, "Possibly-ambiguous questions, no kb_verify_identifier", fc["ambiguous_id_questions_no_verify_tool"]
        )

    ch = audit.get("citation_hygiene")
    if ch:
        out.append("## Citation hygiene")
        out.append("")
        out.append(
            f"- {ch['grounded_count']}/{ch['total']} grounded (every cited doc_id appears in some tool transcript)"
        )
        out.append(f"- {len(ch['no_citations'])} captures with no citations")
        out.append(
            f"- {len(ch['fabricated_citations'])} captures with fabricated citations (cited doc_id not seen in any tool response)"
        )
        out.append("")
        out.append(
            'Note: "seen in transcript" uses a UUID-shape regex over `result_summary`; false positives/negatives possible. Treat as a heuristic.'
        )
        out.append("")
        if ch["no_citations"]:
            out.append("### No-citation captures")
            for nc in ch["no_citations"][:20]:
                out.append(f"- `{nc['qid']}` — {nc['answer_preview'][:120]}")
            out.append("")
        if ch["fabricated_citations"]:
            out.append("### Fabricated-citation captures")
            for fab in ch["fabricated_citations"][:20]:
                out.append(f"- `{fab['qid']}` — cited {fab['cited']}, seen-in-transcript {fab['seen_in_transcript']}")
            out.append("")

    cj = audit.get("cross_judge")
    if cj:
        ja, jb = cj["judges"]
        out.append(f"## Cross-judge — {ja} vs {jb} (Δ = {jb} − {ja}; n={cj['n']})")
        out.append("")
        out.append("| dim | mean Δ | std | min | max | spearman | kappa |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for dim in ("correctness", "groundedness", "completeness"):
            d = cj["deltas"][dim]
            out.append(
                f"| {dim} | {d['mean']:+.2f} | {d['std']:.2f} | {d['min']:+d} | {d['max']:+d} | {cj['spearman'][dim]:.2f} | {cj['kappa'][dim]:.2f} |"
            )
        out.append("")
        if cj["disagreements"]:
            out.append(f"### Top disagreements (|Δ| ≥ 2 on any dimension; {len(cj['disagreements'])} items)")
            out.append("")
            for dis in cj["disagreements"][:20]:
                a, b = dis["judge_a"], dis["judge_b"]
                out.append(
                    f"- `{dis['qid']}` — {dis['delta_dim']} {a[dis['delta_dim']]} ({ja}) vs {b[dis['delta_dim']]} ({jb})"
                )
                out.append(f"  - {ja}: {a.get('rationale', '')[:140]}")
                out.append(f"  - {jb}: {b.get('rationale', '')[:140]}")
            out.append("")

    return "\n".join(out)


def _render_fc_block(out: list[str], title: str, items: list[dict]) -> None:
    out.append(f"### {title} ({len(items)} items)")
    if not items:
        out.append("- (none)")
    for item in items[:20]:
        tools = ", ".join(item.get("tools_used", []))
        corr = item.get("correctness", "?")
        line = f"- `{item['qid']}` — correctness {corr}, tools [{tools}]"
        if item.get("title_fragment"):
            line += f" — {item['title_fragment']}"
        out.append(line)
    out.append("")


if __name__ == "__main__":
    sys.exit(main())
