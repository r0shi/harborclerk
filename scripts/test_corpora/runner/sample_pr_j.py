"""Sample the two PR-J failure populations from existing baseline captures.

Inputs:
  - captures_root: <workdir>/answer-eval/captures/  (per default_workdir())
  - groundtruth_root: scripts/test_corpora/groundtruth/  (the curated yaml set)

Output: scripts/test_corpora/runner/pr_j_populations.json — a frozen sample
that pins which (qid, corpus) pairs the rerun script will re-run.

Two populations:
  - negatives_hedged: qtype=="negative", baseline answer contains hedging
    markers (e.g., "however", "you may be interested", "closest match").
  - finds_short: qtype=="find", truth-doc count >= TRUTH_COUNT_THRESHOLD, and
    the baseline cited_doc_titles length <= CITATION_SHORT_THRESHOLD.

The script is deterministic — same captures + same seed = same population.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
from pathlib import Path

import yaml

log = logging.getLogger("sample_pr_j")

HEDGE_MARKERS: tuple[str, ...] = (
    "however",
    "you may be interested",
    "closest match",
)
TRUTH_COUNT_THRESHOLD = 50
CITATION_SHORT_THRESHOLD = 10


def _load_groundtruth(groundtruth_root: Path, corpus: str) -> dict[str, dict]:
    """Load <corpus>.yaml -> {qid: {qtype, answer_key, ...}}.

    The real groundtruth YAMLs use ``type`` for the question type field;
    test stubs may use ``qtype``.  Both are accepted — ``qtype`` wins if
    both are present.
    """
    yaml_path = groundtruth_root / f"{corpus}.yaml"
    if not yaml_path.exists():
        log.warning("ground-truth missing for corpus=%s at %s", corpus, yaml_path)
        return {}
    data = yaml.safe_load(yaml_path.read_text()) or {}
    # Real groundtruth YAMLs use "items"; test stubs use "questions".
    items = data.get("questions") or data.get("items") or []
    result: dict[str, dict] = {}
    for it in items:
        qtype = it.get("qtype") or it.get("type")
        result[it["id"]] = {**it, "qtype": qtype}
    return result


def _iter_captures(captures_root: Path, *, corpus: str, model: str):
    """Yield each capture dict under captures_root/<corpus>/<model>/*.json."""
    cap_dir = captures_root / corpus / model
    if not cap_dir.exists():
        log.warning("no captures dir at %s", cap_dir)
        return
    for path in sorted(cap_dir.glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable capture %s: %s", path, exc)


def _is_hedged(answer: str) -> bool:
    """Case-insensitive scan for any HEDGE_MARKERS substring."""
    if not answer:
        return False
    lower = answer.lower()
    return any(marker in lower for marker in HEDGE_MARKERS)


def sample_populations(
    *,
    captures_root: Path,
    groundtruth_root: Path,
    corpora: tuple[str, ...],
    model: str,
    max_per_population: int,
    seed: int = 42,
) -> dict[str, list[dict]]:
    """Walk all captures across corpora; bucket into the two populations.

    Returns: {"negatives_hedged": [...], "finds_short": [...]}; each entry is
      {"qid": ..., "corpus": ..., "reason": <bookkeeping>}.
    """
    negatives_hedged: list[dict] = []
    finds_short: list[dict] = []

    for corpus in corpora:
        gt = _load_groundtruth(groundtruth_root, corpus)
        for cap in _iter_captures(captures_root, corpus=corpus, model=model):
            qid = cap.get("question_id")
            gt_item = gt.get(qid)
            if not gt_item:
                continue
            qtype = gt_item.get("qtype")
            answer = cap.get("answer") or ""
            cited_titles = cap.get("cited_doc_titles") or []

            if qtype == "negative" and _is_hedged(answer):
                negatives_hedged.append(
                    {
                        "qid": qid,
                        "corpus": corpus,
                        "reason": "hedge-marker-in-answer",
                    }
                )
            elif qtype == "find":
                ak = gt_item.get("answer_key") or {}
                truth_count = (ak or {}).get("count", 0)
                if truth_count >= TRUTH_COUNT_THRESHOLD and len(cited_titles) <= CITATION_SHORT_THRESHOLD:
                    finds_short.append(
                        {
                            "qid": qid,
                            "corpus": corpus,
                            "reason": f"truth={truth_count}, cited={len(cited_titles)}",
                        }
                    )

    rng = random.Random(seed)
    if len(negatives_hedged) > max_per_population:
        negatives_hedged = rng.sample(negatives_hedged, max_per_population)
    if len(finds_short) > max_per_population:
        finds_short = rng.sample(finds_short, max_per_population)

    # Sort by qid for stable ordering (random.sample is itself deterministic on
    # seeded input, but we serialize sorted for git-diff readability).
    negatives_hedged.sort(key=lambda d: d["qid"])
    finds_short.sort(key=lambda d: d["qid"])

    return {"negatives_hedged": negatives_hedged, "finds_short": finds_short}


def write_populations(
    populations: dict[str, list[dict]],
    out_path: Path,
    *,
    model: str,
    corpora: tuple[str, ...],
) -> None:
    """Serialize with provenance metadata."""
    payload = {
        "model": model,
        "corpora": list(corpora),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "hedge_markers": list(HEDGE_MARKERS),
        "truth_count_threshold": TRUTH_COUNT_THRESHOLD,
        "citation_short_threshold": CITATION_SHORT_THRESHOLD,
        **populations,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")


def _default_workdir() -> Path:
    """Resolve the workdir the same way audit_answer_eval.py does."""
    import os

    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    # macOS native default
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "test-corpora"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s sample_pr_j: %(message)s")
    p = argparse.ArgumentParser(description="Generate pr_j_populations.json from existing captures.")
    p.add_argument("--workdir", type=Path, default=None, help="Override HARBOR_CLERK_WORKDIR")
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Baseline model whose captures we sample from",
    )
    p.add_argument(
        "--corpora",
        nargs="+",
        default=["cuad", "enron", "synthetic"],
        help="Corpora to scan",
    )
    p.add_argument("--max-per-population", type=int, default=25)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("scripts/test_corpora/runner/pr_j_populations.json"),
    )
    args = p.parse_args(argv)

    workdir = args.workdir or _default_workdir()
    captures_root = workdir / "answer-eval" / "captures"
    groundtruth_root = Path("scripts/test_corpora/groundtruth")

    populations = sample_populations(
        captures_root=captures_root,
        groundtruth_root=groundtruth_root,
        corpora=tuple(args.corpora),
        model=args.model,
        max_per_population=args.max_per_population,
        seed=args.seed,
    )
    write_populations(populations, args.out, model=args.model, corpora=tuple(args.corpora))

    log.info(
        "wrote %s — negatives_hedged=%d, finds_short=%d",
        args.out,
        len(populations["negatives_hedged"]),
        len(populations["finds_short"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
