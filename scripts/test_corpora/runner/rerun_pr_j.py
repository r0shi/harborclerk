"""Re-run baselines on the PR-J populations under a labeled capture subdir.

Reads pr_j_populations.json, looks up each qid in <corpus>.yaml, runs the
question through the configured provider, and persists the capture under
  <captures_root>/pr-j-prompt-tuning/<label>/<model>/<qid>.json

Typical use:
  # 1) Generate the populations once.
  uv run python -m scripts.test_corpora.runner.sample_pr_j

  # 2) BEFORE: run on PR-D (no PR-J changes — check out main).
  git checkout main
  uv run python -m scripts.test_corpora.runner.rerun_pr_j --label before-pr-j

  # 3) AFTER: run on PR-J (this branch).
  git checkout feat/pr-j-prompt-tuning
  uv run python -m scripts.test_corpora.runner.rerun_pr_j --label after-pr-j

  # 4) Diff the captures and report deltas in the PR description.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("rerun_pr_j")


def _default_workdir() -> Path:
    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "test-corpora"


def _load_groundtruth(groundtruth_root: Path, corpus: str) -> dict[str, dict]:
    """Load <corpus>.yaml — handles both real schema (items:) and stub schema
    (questions:) at the list level. Returns raw item dicts (no field
    normalization — rerun_populations only reads `question`, not `type`/`qtype`,
    so it does not need the qtype normalization that sample_pr_j applies)."""
    yaml_path = groundtruth_root / f"{corpus}.yaml"
    if not yaml_path.exists():
        log.warning("missing groundtruth %s", yaml_path)
        return {}
    data = yaml.safe_load(yaml_path.read_text()) or {}
    items = data.get("questions") or data.get("items") or []
    return {it["id"]: it for it in items}


def _save_capture(out_dir: Path, qid: str, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{qid}.json").write_text(json.dumps(payload, indent=2) + "\n")


def rerun_populations(
    *,
    populations_path: Path,
    groundtruth_root: Path,
    captures_root: Path,
    label: str,
    model: str,
    provider_factory: Callable[..., Any],
    mcp_session: Any,
) -> None:
    """Drive the experiment.

    provider_factory(model, *, mcp_session) -> object with
      run_question(question, question_id, corpus) -> BaselineResult.
    """
    pop = json.loads(Path(populations_path).read_text())
    items: list[dict] = []
    items.extend(pop.get("negatives_hedged") or [])
    items.extend(pop.get("finds_short") or [])

    if not items:
        log.warning("populations file %s has no items", populations_path)
        return

    # Group by corpus to load ground truth once per corpus.
    by_corpus: dict[str, list[dict]] = {}
    for it in items:
        by_corpus.setdefault(it["corpus"], []).append(it)

    out_dir = captures_root / "pr-j-prompt-tuning" / label / model
    provider = provider_factory(model, mcp_session=mcp_session)

    for corpus, group in by_corpus.items():
        gt = _load_groundtruth(groundtruth_root, corpus)
        for item in group:
            qid = item["qid"]
            gt_item = gt.get(qid)
            if not gt_item:
                log.warning("qid %s not found in %s ground truth — skipping", qid, corpus)
                continue
            question = gt_item.get("question", "")
            try:
                result = provider.run_question(question, qid, corpus)
                _save_capture(out_dir, qid, dataclasses.asdict(result))
                log.info("captured %s (corpus=%s, label=%s)", qid, corpus, label)
            except Exception as exc:
                log.warning("provider failure on %s: %s", qid, exc)
                _save_capture(
                    out_dir,
                    qid,
                    {
                        # BaselineResult-shaped fields (filled with empties so
                        # downstream readers that don't pre-filter on `error`
                        # get safe defaults rather than KeyError).
                        "question_id": qid,
                        "question": question,
                        "answer": "",
                        "cited_doc_ids": [],
                        "cited_doc_titles": [],
                        "tool_call_count": 0,
                        "tool_transcript": [],
                        "elapsed_seconds": 0.0,
                        "model": model,
                        "timestamp": "",
                        # Extras that mark this as a failure capture.
                        "corpus": corpus,
                        "error": str(exc),
                    },
                )


def _make_mcp_session(*, api_base: str, insecure: bool) -> Any:
    """Construct the MCP session the same way answer_eval._live_capture_fn does.

    Resolves auth from env: prefer HC_API_KEY; fall back to HC_USERNAME +
    HC_PASSWORD via HarborClerkClient.login(). Imported lazily so unit tests
    can run without the MCP stack.
    """
    from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession

    token = os.environ.get("HC_API_KEY")
    if not token:
        user, password = os.environ.get("HC_USERNAME"), os.environ.get("HC_PASSWORD")
        if not (user and password):
            raise RuntimeError(
                "rerun_pr_j needs HC_API_KEY (corpus-scoped) or HC_USERNAME + HC_PASSWORD in the environment"
            )
        hc = HarborClerkClient(api_base, verify=not insecure)
        hc.login(user, password)
        token = hc.get_bearer_token()

    mcp_url = os.environ.get("HC_MCP_URL") or f"{api_base}/mcp/mcp"
    return SyncMcpSession(url=mcp_url, headers={"Authorization": f"Bearer {token}"})


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s rerun_pr_j: %(message)s")
    p = argparse.ArgumentParser(description="Re-run baselines on PR-J populations under a labeled subdir.")
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--label", required=True, help="e.g. before-pr-j / after-pr-j")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument(
        "--api-base",
        default=os.environ.get("HC_API_BASE", "https://localhost"),
        help="HC HTTPS base URL (env: HC_API_BASE, default https://localhost)",
    )
    p.add_argument("--insecure", action="store_true", help="Skip TLS verify (self-signed dev cert)")
    p.add_argument(
        "--populations",
        type=Path,
        default=Path("scripts/test_corpora/runner/pr_j_populations.json"),
    )
    p.add_argument(
        "--groundtruth-root",
        type=Path,
        default=Path("scripts/test_corpora/groundtruth"),
    )
    args = p.parse_args(argv)

    workdir = args.workdir or _default_workdir()
    captures_root = workdir / "answer-eval" / "captures"

    # Lazy imports kept here so the test suite can mock provider_factory.
    from scripts.test_corpora.runner.providers import make_provider

    mcp_session = _make_mcp_session(api_base=args.api_base, insecure=args.insecure)

    # Wrap make_provider to reset the per-question `_cited` dict between items
    # (same pattern as answer_eval._live_capture_fn). Without this, cited_doc_ids
    # accumulate across the batch and contaminate later captures.
    def _factory(model: str, *, mcp_session):
        provider = make_provider(model, mcp_session=mcp_session)
        original_run = provider.run_question

        def _run(question: str, question_id: str, corpus: str):
            provider._cited = {}
            return original_run(question=question, question_id=question_id, corpus=corpus)

        provider.run_question = _run
        return provider

    rerun_populations(
        populations_path=args.populations,
        groundtruth_root=args.groundtruth_root,
        captures_root=captures_root,
        label=args.label,
        model=args.model,
        provider_factory=_factory,
        mcp_session=mcp_session,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
