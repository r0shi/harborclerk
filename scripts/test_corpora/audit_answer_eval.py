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
    # audit.md wired in Task 7.
    return 0


if __name__ == "__main__":
    sys.exit(main())
