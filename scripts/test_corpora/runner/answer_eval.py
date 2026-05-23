# scripts/test_corpora/runner/answer_eval.py
"""--mode answer-eval — run a frontier model through HC's MCP over a
ground-truth set and score answers for correctness / groundedness /
completeness against expert labels.

Mirrors retrieval_eval.py. Artifacts are model-keyed and reused by default:
  <workdir>/answer-eval/captures/<corpus>/<model>/<id>.json   (model+MCP run)
  <workdir>/answer-eval/verdicts/<corpus>/<model>/<id>.json   (judge verdict)
  <workdir>/answer-eval/reports/<label>/summary.json          (per-run report)
Regeneration is explicit: --refresh re-runs captures, --rejudge re-runs the
judge. The ground-truth set itself is a frozen, version-controlled file.

scope_folder_ids classification (Task 1): pre-ranking. A folder-scoped API key
restricts the candidate set before ranking — scope.apply_key_scope() builds the
in-scope doc-id set and search.py spreads Chunk.doc_id.in_(doc_ids) into both
the FTS and vector WHERE clauses ahead of ORDER BY/LIMIT. A scoped search over
the 3-corpus index is therefore equivalent to a single-corpus index.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_judge import AnswerJudge, AnswerVerdict

log = logging.getLogger("answer_eval")

GROUNDTRUTH_DIR = Path(__file__).resolve().parents[1] / "groundtruth"


@dataclasses.dataclass(frozen=True)
class GTItem:
    id: str
    question: str
    clause_category: str
    gold_doc: str
    answer_key: str | dict | None
    type: str


def load_groundtruth(path: Path) -> list[GTItem]:
    data = yaml.safe_load(path.read_text())
    items: list[GTItem] = []
    for i in data["items"]:
        ak = i.get("answer_key")
        qtype = i["type"]
        if qtype == "find":
            if not isinstance(ak, dict):
                raise ValueError(
                    f"find item {i['id']!r} answer_key must be a dict with count/all/sample, got {type(ak).__name__}"
                )
            missing = {"count", "all", "sample"} - set(ak)
            if missing:
                raise ValueError(f"find item {i['id']!r} missing required answer_key keys: {sorted(missing)}")
        items.append(
            GTItem(
                id=i["id"],
                question=i["question"],
                clause_category=i["clause_category"],
                gold_doc=i["gold_doc"],
                answer_key=ak,
                type=qtype,
            )
        )
    return items


def aggregate(rows: list[tuple[str, str, AnswerVerdict]]) -> dict:
    """rows = [(item_id, type, verdict)]. Means overall and by type."""

    def _means(group: list[AnswerVerdict]) -> dict:
        # Always returns the full four-key shape (zeros when the group is empty)
        # so callers — including the OVERALL log line — can index every key.
        n = len(group)
        return {
            "n": n,
            "correctness": sum(v.correctness for v in group) / n if n else 0.0,
            "groundedness": sum(v.groundedness for v in group) / n if n else 0.0,
            "completeness": sum(v.completeness for v in group) / n if n else 0.0,
        }

    by_type: dict[str, list[AnswerVerdict]] = {}
    for _id, qtype, v in rows:
        by_type.setdefault(qtype, []).append(v)
    return {
        "overall": _means([v for _, _, v in rows]),
        "by_type": {t: _means(g) for t, g in by_type.items()},
    }


def _cited_text(capture: dict) -> str:
    """Compact rendering of the cited evidence for the judge."""
    titles = capture.get("cited_doc_titles") or []
    transcript = capture.get("tool_transcript") or []
    lines = [f"cited docs: {', '.join(t for t in titles if t)}"]
    for call in transcript:
        lines.append(f"[{call.get('tool')}] {str(call.get('result_summary'))[:400]}")
    return "\n".join(lines)


def compute_coverage(cited: list[str], truth_all: list[str]) -> int:
    """Deterministic coverage score for `find` items, mapped to the 0–5 scale.

    For `find`-negatives (``truth_all == []``): citing nothing is correct (5);
    each false-positive citation costs 1 point, floor 0.
    For `find` items with a non-empty truth: ``|cited ∩ truth| / |truth| * 5``,
    banker's-rounded to an int. Used by ``run()`` to override the judge's
    ``completeness`` for ``find`` items — see the answer-eval phase-2a design
    (Enron).
    """
    if not truth_all:
        return max(0, 5 - len(cited))
    overlap = len(set(cited) & set(truth_all))
    return round(overlap / len(truth_all) * 5)


def run(
    *,
    workdir: Path,
    corpus: str,
    model: str,
    label: str,
    api_base: str,
    refresh: bool,
    rejudge: bool,
    insecure: bool,
    groundtruth_path: Path | None = None,
    capture_fn: Callable[[GTItem], dict] | None = None,
    judge: AnswerJudge | None = None,
) -> int:
    """Returns an exit code (0 = success). `capture_fn` and `judge` are
    injectable for tests; in production they default to a live model+MCP run
    and a live AnswerJudge."""
    gt_path = groundtruth_path or (GROUNDTRUTH_DIR / f"{corpus}.yaml")
    if not gt_path.exists():
        log.error("ground-truth set not found: %s", gt_path)
        return 2
    items = load_groundtruth(gt_path)
    log.info("loaded %d ground-truth items from %s", len(items), gt_path)

    # Build the model/judge clients before creating any output directories, so
    # a missing credential fails fast without leaving empty dirs behind.
    if capture_fn is None:
        capture_fn = _live_capture_fn(api_base=api_base, corpus=corpus, model=model, insecure=insecure)
    if judge is None:
        judge = AnswerJudge()

    cap_dir = workdir / "answer-eval" / "captures" / corpus / model
    ver_dir = workdir / "answer-eval" / "verdicts" / corpus / model
    cap_dir.mkdir(parents=True, exist_ok=True)
    ver_dir.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, AnswerVerdict]] = []
    for item in items:
        cap_path = cap_dir / f"{item.id}.json"
        capture: dict | None = None
        if cap_path.exists() and not refresh:
            try:
                capture = json.loads(cap_path.read_text())
            except json.JSONDecodeError as e:
                log.warning("  %s: unreadable capture %s (%s) — re-running", item.id, cap_path, e)
        if capture is None:
            log.info("capturing %s via model+MCP", item.id)
            capture = capture_fn(item)
            cap_path.write_text(json.dumps(capture, indent=2))

        ver_path = ver_dir / f"{item.id}.json"
        verdict: AnswerVerdict | None = None
        if ver_path.exists() and not rejudge and not refresh:
            try:
                verdict = AnswerVerdict(**json.loads(ver_path.read_text()))
            except (json.JSONDecodeError, TypeError) as e:
                log.warning("  %s: unreadable verdict %s (%s) — re-judging", item.id, ver_path, e)
        if verdict is None:
            verdict = judge.judge_answer(
                question=item.question,
                model_answer=capture.get("answer", ""),
                cited=_cited_text(capture),
                answer_key=item.answer_key,
                qtype=item.type,
            )
            if item.type == "find":
                truth_all = item.answer_key.get("all", []) if isinstance(item.answer_key, dict) else []
                coverage = compute_coverage(capture.get("cited_doc_titles", []) or [], truth_all)
                verdict = dataclasses.replace(
                    verdict,
                    completeness=coverage,
                    source={**verdict.source, "completeness": "deterministic"},
                )
            ver_path.write_text(json.dumps(dataclasses.asdict(verdict), indent=2))
        log.info(
            "  %s correctness=%d grounded=%d complete=%d",
            item.id,
            verdict.correctness,
            verdict.groundedness,
            verdict.completeness,
        )
        rows.append((item.id, item.type, verdict))

    summary = aggregate(rows)
    report_dir = workdir / "answer-eval" / "reports" / label
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (report_dir / "detail.json").write_text(
        json.dumps([{"id": i, "type": t, **dataclasses.asdict(v)} for i, t, v in rows], indent=2)
    )
    o = summary["overall"]
    log.info(
        "OVERALL n=%d correctness=%.2f groundedness=%.2f completeness=%.2f",
        o["n"],
        o["correctness"],
        o["groundedness"],
        o["completeness"],
    )
    return 0


def _live_capture_fn(*, api_base: str, corpus: str, model: str, insecure: bool) -> Callable[[GTItem], dict]:
    """Build the production capture function: a model+MCP run per item.

    Reuses the phase-1 machinery — SyncMcpSession for the MCP tool calls,
    BaselineGenerator for the agentic loop. The MCP session MUST be
    authenticated with a corpus-scoped API key so HC's search is restricted to
    `corpus`: set HC_API_KEY to that scoped key. Falls back to a
    HC_USERNAME/HC_PASSWORD login, which yields an UNSCOPED (full-index)
    session — correct only when `corpus` is the lone corpus loaded.
    """
    import anthropic

    from scripts.test_corpora.runner.claude_baseline import BaselineGenerator
    from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession

    token = os.environ.get("HC_API_KEY")
    if not token:
        user, password = os.environ.get("HC_USERNAME"), os.environ.get("HC_PASSWORD")
        if not (user and password):
            raise RuntimeError(
                "answer-eval needs HC_API_KEY (a corpus-scoped key) or HC_USERNAME + HC_PASSWORD in the environment"
            )
        hc = HarborClerkClient(api_base, verify=not insecure)
        hc.login(user, password)
        token = hc.get_bearer_token()
        if not token:
            raise RuntimeError("HC login did not yield a bearer token — check HC_USERNAME / HC_PASSWORD")
        log.warning("HC_API_KEY unset — using an unscoped login; search is NOT corpus-restricted")

    mcp_url = os.environ.get("HC_MCP_URL") or f"{api_base}/mcp/mcp"
    mcp = SyncMcpSession(url=mcp_url, headers={"Authorization": f"Bearer {token}"})
    anthro = anthropic.Anthropic()

    def capture(item: GTItem) -> dict:
        gen = BaselineGenerator(client=anthro, mcp_session=mcp, model=model)
        res = gen.run_question(question=item.question, question_id=item.id, corpus=corpus)
        return dataclasses.asdict(res)

    return capture


def add_cli_args(p: argparse.ArgumentParser) -> None:
    """Register answer-eval-only flags. --label / --run-id / --workdir /
    --api-base / --corpora / --models / --insecure already exist on the sweep
    parser (the last set via retrieval_eval.add_cli_args)."""
    g = p.add_argument_group("answer-eval (only with --mode answer-eval)")
    g.add_argument("--refresh", action="store_true", help="re-run model+MCP captures even if present")
    g.add_argument("--rejudge", action="store_true", help="re-run the judge even if verdicts are present")


def main_from_args(args: argparse.Namespace) -> int:
    corpus = args.corpora.split(",")[0].strip() if args.corpora else "cuad"
    model = args.models.split(",")[0].strip() if args.models else "claude-sonnet-4-6"
    if (args.corpora and "," in args.corpora) or (args.models and "," in args.models):
        log.warning(
            "answer-eval is single-corpus/single-model in phase 1; using corpus=%s model=%s, ignoring the rest",
            corpus,
            model,
        )
    label = getattr(args, "label", None)
    if not label:
        log.error("--label is required for --mode answer-eval")
        return 2
    return run(
        workdir=Path(args.workdir),
        corpus=corpus,
        model=model,
        label=label,
        api_base=args.api_base,
        refresh=args.refresh,
        rejudge=args.rejudge,
        insecure=args.insecure,
    )
