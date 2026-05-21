"""--mode retrieval-eval — fast retrieval-only evaluation between phases.

The full 6-phase sweep (sweep.py) takes hours and runs every local model
across every corpus. This mode bypasses all of that to answer one question:

    "Did my retrieval-pipeline change move recall vs the existing baselines?"

For each baseline JSON written by Phase 1, it POSTs the question text to
HC's ``/api/search``, dedupes the doc_ids by best score, and computes
``recall@k`` / ``MRR`` / ``nDCG@10`` against the baseline's
``cited_doc_ids``. No LLM is invoked. No model switch. No re-ingest. No
``state.json``.

CLI surface (dispatched from sweep.py when ``--mode retrieval-eval`` is set):

    sweep --mode retrieval-eval \\
        --run-id 2026-05-05-prod \\
        --api-base http://localhost:8100 \\
        --label phase-0-granite-r2 \\
        [--prior-label e5-small-baseline] \\
        [--corpora cuad,synthetic] \\
        [--questions-per-corpus N] \\
        [--k 5,10,20] \\
        [--search-k 50]

Output under ``<workdir>/results/<run_id>/retrieval-eval/<label>/``:

  * ``metrics.csv``   — one row per question
  * ``summary.json``  — overall + per-corpus aggregates
  * ``vs_<prior_label>.json`` — pairwise delta (only if ``--prior-label`` set)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.metrics import ndcg_at_k, recall_at_k, reciprocal_rank
from scripts.test_corpora.runner.quality import baseline_quality_problem

log = logging.getLogger("retrieval_eval")


# ── data shapes ──


@dataclasses.dataclass(frozen=True)
class BaselineRecord:
    corpus: str
    question_id: str
    question_text: str
    cited_doc_ids: list[str]


@dataclasses.dataclass
class EvalRow:
    corpus: str
    question_id: str
    question_text: str
    recall_at_k: dict[int, float]
    reciprocal_rank: float
    ndcg_at_10: float
    top_doc_id: str | None
    baseline_cited_doc_ids: list[str]
    returned_doc_ids: list[str]


# ── pure functions ──


def load_baselines(
    baselines_dir: Path,
    corpora: set[str] | None,
    limit_per_corpus: int | None,
) -> list[BaselineRecord]:
    """Walk ``baselines/<corpus>/*.json``, drop unusable rows, return records.

    Unusable means ``cited_doc_ids`` is empty (the baseline can't grade
    retrieval) or any of the existing ``baseline_quality_problem`` checks
    flag the baseline as corrupt/refusal/placeholder.
    """
    if not baselines_dir.exists():
        return []

    records: list[BaselineRecord] = []
    per_corpus_count: dict[str, int] = {}

    for corpus_dir in sorted(p for p in baselines_dir.iterdir() if p.is_dir()):
        corpus = corpus_dir.name
        if corpora is not None and corpus not in corpora:
            continue
        for path in sorted(corpus_dir.glob("*.json")):
            data = json.loads(path.read_text())
            cited = data.get("cited_doc_ids") or []
            if not cited:
                continue
            if baseline_quality_problem(data):
                continue
            if limit_per_corpus is not None and per_corpus_count.get(corpus, 0) >= limit_per_corpus:
                break
            records.append(
                BaselineRecord(
                    corpus=corpus,
                    question_id=data["question_id"],
                    question_text=data["question"],
                    cited_doc_ids=list(cited),
                )
            )
            per_corpus_count[corpus] = per_corpus_count.get(corpus, 0) + 1

    return records


def score_one(baseline: BaselineRecord, ranked_doc_ids: list[str], ks: list[int]) -> EvalRow:
    """Compute the per-question retrieval metrics."""
    return EvalRow(
        corpus=baseline.corpus,
        question_id=baseline.question_id,
        question_text=baseline.question_text,
        recall_at_k={k: recall_at_k(baseline.cited_doc_ids, ranked_doc_ids, k) for k in ks},
        reciprocal_rank=reciprocal_rank(baseline.cited_doc_ids, ranked_doc_ids),
        ndcg_at_10=ndcg_at_k(baseline.cited_doc_ids, ranked_doc_ids, 10),
        top_doc_id=ranked_doc_ids[0] if ranked_doc_ids else None,
        baseline_cited_doc_ids=list(baseline.cited_doc_ids),
        returned_doc_ids=list(ranked_doc_ids),
    )


def aggregate(rows: list[EvalRow], ks: list[int]) -> dict[str, Any]:
    """Overall + per-corpus mean of each metric."""

    def _summary(subset: list[EvalRow]) -> dict[str, Any]:
        if not subset:
            return {"n_questions": 0, "recall_at_k": {k: 0.0 for k in ks}, "mrr": 0.0, "ndcg_at_10": 0.0}
        n = len(subset)
        return {
            "n_questions": n,
            "recall_at_k": {k: sum(r.recall_at_k[k] for r in subset) / n for k in ks},
            "mrr": sum(r.reciprocal_rank for r in subset) / n,
            "ndcg_at_10": sum(r.ndcg_at_10 for r in subset) / n,
        }

    per_corpus: dict[str, dict[str, Any]] = {}
    for r in rows:
        per_corpus.setdefault(r.corpus, []).append(r)
    return {
        "overall": _summary(rows),
        "per_corpus": {c: _summary(rs) for c, rs in per_corpus.items()},
    }


def compute_diff(
    current: list[EvalRow],
    prior: list[EvalRow],
    regression_threshold: float,
) -> dict[str, Any]:
    """Pairwise delta between two labeled eval runs.

    A question whose ``recall_at_10`` drops by more than
    ``regression_threshold`` is a regression. The same magnitude in the
    other direction is an improvement. Questions only in one side are
    bucketed separately so they don't look like noise.
    """
    by_key_current = {(r.corpus, r.question_id): r for r in current}
    by_key_prior = {(r.corpus, r.question_id): r for r in prior}

    common = set(by_key_current) & set(by_key_prior)
    only_current = sorted(set(by_key_current) - set(by_key_prior))
    only_prior = sorted(set(by_key_prior) - set(by_key_current))

    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for key in sorted(common):
        cur = by_key_current[key]
        pri = by_key_prior[key]
        delta_recall = cur.recall_at_k[10] - pri.recall_at_k[10]
        delta_mrr = cur.reciprocal_rank - pri.reciprocal_rank
        delta_ndcg = cur.ndcg_at_10 - pri.ndcg_at_10
        entry = {
            "corpus": cur.corpus,
            "question_id": cur.question_id,
            "recall_at_10_current": cur.recall_at_k[10],
            "recall_at_10_prior": pri.recall_at_k[10],
            "recall_at_10_delta": delta_recall,
            "mrr_delta": delta_mrr,
            "ndcg_at_10_delta": delta_ndcg,
        }
        if delta_recall <= -regression_threshold:
            regressions.append(entry)
        elif delta_recall >= regression_threshold:
            improvements.append(entry)

    def _mean(rs, attr):
        return sum(getattr(r, attr) for r in rs) / len(rs) if rs else 0.0

    def _mean_recall(rs):
        return sum(r.recall_at_k[10] for r in rs) / len(rs) if rs else 0.0

    cur_common = [by_key_current[k] for k in common]
    pri_common = [by_key_prior[k] for k in common]
    overall_deltas = {
        "recall_at_10": _mean_recall(cur_common) - _mean_recall(pri_common),
        "mrr": _mean(cur_common, "reciprocal_rank") - _mean(pri_common, "reciprocal_rank"),
        "ndcg_at_10": _mean(cur_common, "ndcg_at_10") - _mean(pri_common, "ndcg_at_10"),
    }

    return {
        "regression_threshold": regression_threshold,
        "n_common_questions": len(common),
        "overall_deltas": overall_deltas,
        "regressions": regressions,
        "improvements": improvements,
        "new_questions": [{"corpus": c, "question_id": q} for c, q in only_current],
        "dropped_questions": [{"corpus": c, "question_id": q} for c, q in only_prior],
    }


# ── HTTP-calling pieces (thin wrappers, so the driver stays mockable) ──


def search_doc_ids(client: HarborClerkClient, query: str, k: int) -> list[str]:
    """POST ``/api/search`` with ``faceted=true`` and return doc_ids ordered
    by best chunk score within each doc.

    ``faceted=true`` groups hits by doc and sorts by top_score, which is
    exactly the ranked list we want — one entry per doc_id, descending by
    relevance. Falls back to per-chunk dedup if the server returns the
    non-faceted shape for any reason.
    """
    r = client._client.post(
        "/api/search",
        json={"query": query, "k": k, "faceted": True},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if "documents" in body:
        return [d["doc_id"] for d in body["documents"]]
    # Non-faceted fallback — dedup by first appearance.
    seen: list[str] = []
    seen_set: set[str] = set()
    for hit in body.get("hits", []):
        did = hit.get("doc_id")
        if did and did not in seen_set:
            seen.append(did)
            seen_set.add(did)
    return seen


# ── driver ──


def _write_csv(rows: list[EvalRow], path: Path, ks: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        header = (
            ["corpus", "question_id"]
            + [f"recall_at_{k}" for k in ks]
            + ["mrr", "ndcg_at_10", "top_doc_id", "n_baseline_cites", "n_returned"]
        )
        w.writerow(header)
        for r in rows:
            w.writerow(
                [r.corpus, r.question_id]
                + [f"{r.recall_at_k[k]:.3f}" for k in ks]
                + [
                    f"{r.reciprocal_rank:.3f}",
                    f"{r.ndcg_at_10:.3f}",
                    r.top_doc_id or "",
                    len(r.baseline_cited_doc_ids),
                    len(r.returned_doc_ids),
                ]
            )


def _load_prior_label(eval_dir: Path, prior_label: str) -> list[EvalRow]:
    """Reconstruct EvalRows from a prior label's metrics.csv.

    We persist enough in the CSV to reconstruct the metric values; baseline
    doc_ids and returned doc_ids aren't needed for diff math, so we leave
    them empty in the reconstructed row.
    """
    path = eval_dir / prior_label / "metrics.csv"
    if not path.exists():
        raise FileNotFoundError(f"prior label not found: {path}")
    rows: list[EvalRow] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        ks = sorted(int(c.split("_")[-1]) for c in reader.fieldnames or [] if c.startswith("recall_at_"))
        for row in reader:
            rows.append(
                EvalRow(
                    corpus=row["corpus"],
                    question_id=row["question_id"],
                    question_text="",
                    recall_at_k={k: float(row[f"recall_at_{k}"]) for k in ks},
                    reciprocal_rank=float(row["mrr"]),
                    ndcg_at_10=float(row["ndcg_at_10"]),
                    top_doc_id=row["top_doc_id"] or None,
                    baseline_cited_doc_ids=[],
                    returned_doc_ids=[],
                )
            )
    return rows


def run(
    *,
    workdir: Path,
    run_id: str,
    api_base: str,
    label: str,
    prior_label: str | None,
    corpora: set[str] | None,
    questions_per_corpus: int | None,
    ks: list[int],
    search_k: int,
    insecure: bool,
    regression_threshold: float = 0.05,
    search_fn: Callable[[HarborClerkClient, str, int], list[str]] | None = None,
) -> int:
    """Main entry point. Returns process exit code."""
    run_dir = workdir / cfg.RESULTS_DIR_NAME / run_id
    baselines_dir = run_dir / "baselines"
    eval_dir = run_dir / "retrieval-eval"
    out_dir = eval_dir / label
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("loading baselines from %s", baselines_dir)
    records = load_baselines(baselines_dir, corpora=corpora, limit_per_corpus=questions_per_corpus)
    if not records:
        log.error(
            "no usable baselines under %s (corpora filter=%s, limit=%s). Run phase 1 first, or check the path.",
            baselines_dir,
            sorted(corpora) if corpora else "all",
            questions_per_corpus,
        )
        return 1
    log.info("found %d usable baselines across %d corpora", len(records), len({r.corpus for r in records}))

    if max(ks) > search_k:
        log.error("--search-k=%d must be >= max(--k)=%d", search_k, max(ks))
        return 2

    # HC client + auth
    hc = HarborClerkClient(api_base, verify=not insecure)
    email = os.environ.get("HC_USERNAME")
    password = os.environ.get("HC_PASSWORD")
    if email and password:
        hc.login(email, password)
    else:
        log.warning("no HC_USERNAME / HC_PASSWORD set — /api/search requires auth")

    do_search = search_fn or search_doc_ids

    rows: list[EvalRow] = []
    for i, rec in enumerate(records, start=1):
        try:
            ranked = do_search(hc, rec.question_text, search_k)
        except httpx.HTTPError as exc:
            log.error("search failed for %s/%s: %r — recording zero metrics", rec.corpus, rec.question_id, exc)
            ranked = []
        row = score_one(rec, ranked, ks=ks)
        rows.append(row)
        if i % 10 == 0 or i == len(records):
            log.info(
                "  [%d/%d] %s/%s recall@10=%.2f mrr=%.2f",
                i,
                len(records),
                rec.corpus,
                rec.question_id,
                row.recall_at_k.get(10, 0.0),
                row.reciprocal_rank,
            )

    metrics_path = out_dir / "metrics.csv"
    _write_csv(rows, metrics_path, ks=ks)
    log.info("wrote per-question metrics → %s", metrics_path)

    summary = aggregate(rows, ks=ks)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    log.info("wrote summary → %s", summary_path)

    overall = summary["overall"]
    log.info(
        "OVERALL  n=%d  recall@10=%.3f  mrr=%.3f  ndcg@10=%.3f",
        overall["n_questions"],
        overall["recall_at_k"].get(10, 0.0),
        overall["mrr"],
        overall["ndcg_at_10"],
    )
    for corpus, s in summary["per_corpus"].items():
        log.info(
            "  %-10s  n=%-3d  recall@10=%.3f  mrr=%.3f  ndcg@10=%.3f",
            corpus,
            s["n_questions"],
            s["recall_at_k"].get(10, 0.0),
            s["mrr"],
            s["ndcg_at_10"],
        )

    if prior_label:
        try:
            prior_rows = _load_prior_label(eval_dir, prior_label)
        except FileNotFoundError as exc:
            log.error("--prior-label specified but not found: %s", exc)
            return 3
        diff = compute_diff(rows, prior_rows, regression_threshold=regression_threshold)
        diff_path = out_dir / f"vs_{prior_label}.json"
        diff_path.write_text(json.dumps(diff, indent=2))
        log.info("wrote diff vs %s → %s", prior_label, diff_path)
        log.info(
            "DIFF vs %s  n_common=%d  Δrecall@10=%+.3f  Δmrr=%+.3f  regressions=%d  improvements=%d",
            prior_label,
            diff["n_common_questions"],
            diff["overall_deltas"]["recall_at_10"],
            diff["overall_deltas"]["mrr"],
            len(diff["regressions"]),
            len(diff["improvements"]),
        )

    return 0


def add_cli_args(parser: argparse.ArgumentParser) -> None:
    """Attach retrieval-eval flags to an existing ArgumentParser.

    Called by sweep.py to register these args alongside the main sweep flags
    so users see them in --help. Most flags are ignored when --mode is unset.
    """
    g = parser.add_argument_group("retrieval-eval (only with --mode retrieval-eval)")
    g.add_argument("--label", help="name this eval run (e.g. 'phase-0-granite-r2'); becomes the output dir name")
    g.add_argument("--prior-label", help="prior eval label to diff against; produces vs_<prior_label>.json")
    g.add_argument(
        "--questions-per-corpus",
        type=int,
        default=None,
        help="cap baselines per corpus (default: all). Useful for ~30s sanity-check runs between micro-phases.",
    )
    g.add_argument(
        "--k",
        default="5,10,20",
        help="comma-separated K values for recall@K (default: 5,10,20). nDCG is always @10.",
    )
    g.add_argument(
        "--search-k",
        type=int,
        default=50,
        help="how many results to fetch from /api/search (must be >= max(--k)). Default: 50.",
    )
    g.add_argument(
        "--regression-threshold",
        type=float,
        default=0.05,
        help="recall@10 delta below -threshold counts as a regression in vs_<prior>.json (default: 0.05).",
    )


def main_from_args(args: argparse.Namespace) -> int:
    """Translate parsed args into a ``run()`` call. Caller validates ``args.mode``."""
    if not args.label:
        log.error("--mode retrieval-eval requires --label")
        return 2

    ks = sorted({int(x) for x in args.k.split(",") if x.strip()})
    if not ks:
        log.error("--k parsed to empty list")
        return 2

    corpora: set[str] | None = None
    if args.corpora:
        corpora = {c.strip() for c in args.corpora.split(",") if c.strip()}

    return run(
        workdir=Path(args.workdir).expanduser(),
        run_id=args.run_id,
        api_base=args.api_base,
        label=args.label,
        prior_label=args.prior_label,
        corpora=corpora,
        questions_per_corpus=args.questions_per_corpus,
        ks=ks,
        search_k=args.search_k,
        insecure=args.insecure,
        regression_threshold=args.regression_threshold,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="retrieval-eval")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workdir", default=str(cfg.WORKDIR_DEFAULT))
    parser.add_argument("--api-base", default=cfg.API_BASE)
    parser.add_argument("--corpora", default="")
    parser.add_argument("--insecure", action="store_true")
    add_cli_args(parser)
    parsed = parser.parse_args()
    sys.exit(main_from_args(parsed))
