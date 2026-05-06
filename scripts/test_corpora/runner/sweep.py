"""Six-phase test sweep entrypoint.

Reads/writes ``state.json`` in the run-id directory. Schedules the next
pending unit, dispatches to the right handler, updates state, repeats.

Phases:
  0. acquire   — download / generate the three corpora
  1. baseline  — Sonnet 4.6 + Harbor Clerk MCP, save baselines/<corpus>/<q>.json
  2. smoke     — one large model × one corpus, iterate on bugs
  3. depth     — same model × all three depths
  4. models    — all 8 × all 3 corpora × standard, completion-only
  5. parity    — top 2 × all 3, mechanical + Sonnet judge
  6. unified   — drop DB, ingest all 3 into one DB, top 2 only

The CLI surface mirrors the design doc:
    --run-id, --workdir, --api-base, --resume, --rerun, --skip,
    --phases, --models, --corpora, --depth
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

import anthropic
import httpx
import yaml

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.corpora import cuad, enron, synthetic
from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.claude_baseline import BaselineGenerator
from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession
from scripts.test_corpora.runner.judge import JudgeClient
from scripts.test_corpora.runner.metrics import citation_extra, citation_overlap, entity_overlap
from scripts.test_corpora.runner.sampler import CompletionEvent, Sampler
from scripts.test_corpora.runner.state import StateFile, Status, Unit

log = logging.getLogger("sweep")


# ── argparse ──


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sweep")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workdir", default=str(cfg.WORKDIR_DEFAULT))
    p.add_argument("--api-base", default=cfg.API_BASE)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--rerun", default="")
    p.add_argument("--skip", default="")
    p.add_argument("--phases", default="0-6")
    p.add_argument("--models", default="")
    p.add_argument("--corpora", default="")
    p.add_argument("--depth", default=cfg.DEFAULT_DEPTH)
    p.add_argument("--time-limit-minutes", type=int, default=30)
    p.add_argument("--insecure", action="store_true", help="disable TLS verify (self-signed)")
    p.add_argument("--dry-run", action="store_true", help="run Phase 0 only, no API calls beyond acquire")
    return p


def _parse_selectors(s: str) -> dict[str, str]:
    if not s:
        return {}
    return dict(part.split("=", 1) for part in s.split(",") if "=" in part)


def _phase_range(s: str) -> set[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return set(range(int(a), int(b) + 1))
    return {int(p) for p in s.split(",") if p}


# ── state planning ──


def _question_ids(corpus_questions: dict) -> list[str]:
    """Return all question ids in a corpus's YAML, expanding cross-language pairs to lang-suffixed ids."""
    ids: list[str] = []
    for kind in ("research", "ask"):
        for q in corpus_questions.get(kind, []):
            if q.get("cross_language"):
                for v in q["variants"]:
                    ids.append(f"{q['id']}__{v['lang']}")
            else:
                ids.append(q["id"])
    return ids


def _question_text(corpus_questions: dict, question_id: str) -> tuple[str, str]:
    """Resolve a (possibly cross-language) question_id to (text, lang)."""
    base, _, lang_suffix = question_id.partition("__")
    for kind in ("research", "ask"):
        for q in corpus_questions.get(kind, []):
            if q["id"] == base:
                if q.get("cross_language"):
                    for v in q["variants"]:
                        if v["lang"] == lang_suffix:
                            return v["text"], v["lang"]
                return q["text"], "en"
    raise KeyError(f"unknown question id {question_id}")


def _is_research(question_id: str) -> bool:
    return "research" in question_id


def _find_owning_corpus(question_id: str, questions_by_corpus: dict) -> str:
    base = question_id.split("__")[0]
    for c, qs in questions_by_corpus.items():
        for kind in ("research", "ask"):
            for q in qs.get(kind, []):
                if q["id"] == base:
                    return c
    raise KeyError(f"no owning corpus for {question_id}")


def _plan_units(
    questions_by_corpus: dict[str, dict],
    phases: set[int],
    depth: str,
    models_filter: set[str] | None = None,
) -> list[Unit]:
    """Generate the full Unit set for the sweep — every cell across all phases.

    ``models_filter`` (when set) restricts the model loops in phases 2-6 to
    that subset. Useful for splitting Phase 4 across machines: a 32 GB Mac
    mini runs the smaller 6 models while a bigger machine runs Gemma 26B
    + Qwen3.6 35B in parallel.
    """
    units: list[Unit] = []
    corpora = list(questions_by_corpus)

    def _phase4_models() -> list[str]:
        return [m for m in cfg.ALL_MODELS if models_filter is None or m in models_filter]

    def _phase5_models() -> list[str]:
        return [m for m in cfg.TOP_MODELS if models_filter is None or m in models_filter]

    for phase in sorted(phases):
        if phase == 0:
            for c in corpora:
                units.append(Unit(phase=0, corpus=c, model="-", question_id="-", depth="-"))
        elif phase == 1:
            for c, qs in questions_by_corpus.items():
                for q in _question_ids(qs):
                    units.append(Unit(phase=1, corpus=c, model="claude-baseline", question_id=q, depth="n/a"))
        elif phase == 2:
            # smoke — one model, one corpus. Skipped if cuad or qwen3.6-35b isn't in scope.
            if "cuad" in questions_by_corpus and (models_filter is None or "qwen3.6-35b" in models_filter):
                units.append(
                    Unit(phase=2, corpus="cuad", model="qwen3.6-35b", question_id="cuad-research-1", depth=depth)
                )
        elif phase == 3:
            # depth coverage — same model × all three depths on cuad. Skipped if cuad or
            # qwen3.6-35b isn't in scope.
            if "cuad" in questions_by_corpus and (models_filter is None or "qwen3.6-35b" in models_filter):
                for d in cfg.DEPTHS:
                    for q in _question_ids(questions_by_corpus["cuad"]):
                        units.append(Unit(phase=3, corpus="cuad", model="qwen3.6-35b", question_id=q, depth=d))
        elif phase == 4:
            # Corpus is outer loop so each corpus change (= full re-ingest) happens only
            # once per corpus rather than once per (model, corpus) pair.
            for c, qs in questions_by_corpus.items():
                for m in _phase4_models():
                    for q in _question_ids(qs):
                        units.append(Unit(phase=4, corpus=c, model=m, question_id=q, depth=depth))
        elif phase == 5:
            # Same rationale: 3 re-ingests (one per corpus) instead of 6 (one per model×corpus).
            for c, qs in questions_by_corpus.items():
                for m in _phase5_models():
                    for q in _question_ids(qs):
                        units.append(Unit(phase=5, corpus=c, model=m, question_id=q, depth=depth))
        elif phase == 6:
            # unified pass — first 3 research questions from each available corpus
            for m in _phase5_models():
                unified_qs: list[str] = []
                for c in ("cuad", "enron", "synthetic"):
                    if c in questions_by_corpus:
                        unified_qs.extend(_question_ids(questions_by_corpus[c])[:3])
                for q in unified_qs:
                    units.append(Unit(phase=6, corpus="unified", model=m, question_id=q, depth=depth))
    return units


# ── phase handlers ──


def _phase0_acquire(corpus_id: str, workdir: Path) -> CorpusManifest:
    if corpus_id == "cuad":
        return cuad.acquire(workdir / "cuad")
    if corpus_id == "enron":
        return enron.acquire(workdir / "enron")
    if corpus_id == "synthetic":
        return synthetic.acquire(workdir / "synthetic")
    raise ValueError(f"unknown corpus {corpus_id}")


def _phase1_baseline(
    client: anthropic.Anthropic, mcp_session, corpus: str, question_id: str, question_text: str, results_dir: Path
) -> dict:
    gen = BaselineGenerator(client=client, mcp_session=mcp_session)
    res = gen.run_question(question=question_text, question_id=question_id, corpus=corpus)
    BaselineGenerator.write(res, results_dir, corpus)
    return dataclasses.asdict(res)


def _run_local(
    hc: HarborClerkClient,
    corpus: str,
    model: str,
    question_id: str,
    question_text: str,
    depth: str,
    time_limit_minutes: int,
    is_research: bool,
    results_dir: Path,
) -> dict:
    """Run one local-model question. Assumes the right model is already active and loaded."""
    if is_research:
        # Clean up any orphan research task from a prior killed sweep before
        # POSTing a new one — otherwise Harbor Clerk returns 409 Conflict
        # and the harness wedges. Idempotent: no-op when nothing is active.
        orphan = hc.cleanup_orphan_research()
        if orphan:
            log.warning("cleaned up orphan research task %s before starting new one", orphan)
        # run_research drains the SSE stream until the research finishes
        # server-side. Closing the stream early would trigger HC's
        # "interrupted-on-disconnect" handler and produce empty results.
        conv_id, result = hc.run_research(question_text, depth=depth, time_limit_minutes=time_limit_minutes)
        # Normalize: ResearchDetail returns "report", chat returns "answer".
        # Store under "answer" so all downstream metrics code uses a uniform key.
        normalized_answer = result.get("report") or result.get("answer", "")
        result["answer"] = normalized_answer
    else:
        # Ask flow: create a chat conversation, then send the question, drain SSE
        conv_id = hc.create_conversation(mode="chat")
        events = list(hc.stream_ask(conv_id, question_text))
        # Harbor Clerk chat SSE emits {type: "token", content: "<text>"} for tokens.
        # Citations are in the final {type: "done"} event's rag_context.citations field.
        final_text = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        citations: list[dict] = []
        for e in events:
            if e.get("type") == "done":
                rc = e.get("rag_context") or {}
                for c in rc.get("citations", []) or e.get("citations", []):
                    citations.append(c if isinstance(c, dict) else {"doc_id": c})
        result = {"status": "completed", "answer": final_text, "citations": citations, "conversation_id": conv_id}

    out = {
        "corpus": corpus,
        "model": model,
        "question_id": question_id,
        "depth": depth,
        "result": result,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path = results_dir / "responses" / corpus / model / f"{question_id}__{depth}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    return out


# ── ingestion helper ──


def _ingest_corpus(hc: HarborClerkClient, manifest: CorpusManifest) -> None:
    """Wipe the DB, register the corpus's ingest dir, wait for ingestion to
    fully complete before returning.

    Three-phase wait closes the race where the harness checks ``pipeline_quiet``
    in the brief window between ``watch_folder_add`` returning and the
    watcher actually scanning the folder. Without this, the harness would
    declare the corpus ingested and start research against zero documents.
    """
    log.info("clearing existing watch folders before ingesting %s", manifest.corpus_id)
    for folder in hc.watch_folder_list():
        hc.watch_folder_delete(folder["folder_id"])
    log.info("delete_all_documents before ingesting %s", manifest.corpus_id)
    hc.delete_all_documents(confirm=True)
    log.info("adding watch folder for %s", manifest.ingest_dir)
    hc.watch_folder_add(str(manifest.ingest_dir), name=f"test-corpora-{manifest.corpus_id}")

    # Phase 1: confirm watcher actually started enqueueing. Some corpora
    # (CUAD's 80 PDFs) can finish ingesting in under the 30s poll interval,
    # so this is essential — without it, the next poll could see an empty
    # queue before the queue ever filled and proceed.
    log.info("waiting for watcher to begin enqueueing files for %s", manifest.corpus_id)
    if not hc.wait_for_pipeline_activity(max_wait_seconds=120):
        raise RuntimeError(
            f"watcher never enqueued any jobs for {manifest.corpus_id} within 120s — "
            f"verify the ingest dir contains supported files: {manifest.ingest_dir}"
        )

    # Phase 2: wait for the queue to drain (existing behaviour).
    log.info("waiting for pipeline to drain (this can take a while)")
    if not hc.wait_for_quiet_pipeline(max_wait_seconds=4 * 3600):
        raise RuntimeError(f"pipeline never drained for {manifest.corpus_id}")

    # Phase 3: warning-level sanity check on document count. Treated as
    # informational because the synthetic corpus has JSON sidecars in the
    # ingest dir that HC may or may not ingest depending on its allowed
    # extensions, so an exact match isn't guaranteed.
    actual = hc.document_count()
    if actual < manifest.doc_count * 0.5:
        log.error(
            "ingest looks incomplete for %s: HC has %d active docs, manifest expected %d",
            manifest.corpus_id,
            actual,
            manifest.doc_count,
        )
    else:
        log.info(
            "ingest verified for %s: HC has %d active docs (manifest expected %d)",
            manifest.corpus_id,
            actual,
            manifest.doc_count,
        )


# ── model switch helper ──


def _ensure_model(hc: HarborClerkClient, current_model: str | None, target_model: str) -> str:
    """Activate target_model if it differs from current_model. Returns the new current_model."""
    if current_model == target_model:
        return current_model
    log.info("activating model %s (was %s)", target_model, current_model)
    hc.activate_model(target_model)
    if not hc.wait_for_model_ready(target_model, max_wait_seconds=600):
        raise RuntimeError(f"model {target_model} did not become ready")
    return target_model


# ── main ──


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    workdir = Path(args.workdir).expanduser()
    run_dir = workdir / cfg.RESULTS_DIR_NAME / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(run_dir / "log.txt"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    state_path = run_dir / "state.json"
    sf = StateFile(state_path)
    sf.acquire_lock()
    try:
        sf.load()

        # Load question YAML for each corpus
        questions_by_corpus = {}
        for c in ("cuad", "enron", "synthetic"):
            q_path = Path(__file__).parent.parent / "questions" / f"{c}.yaml"
            questions_by_corpus[c] = yaml.safe_load(q_path.read_text())

        # Apply --corpora filter: scope plan_units to listed corpora only.
        # Useful for first-time smoke runs that want to skip the synthetic
        # corpus (which costs ~$3-5 in Anthropic API spend to generate).
        if args.corpora:
            requested = {c.strip() for c in args.corpora.split(",") if c.strip()}
            unknown = requested - set(questions_by_corpus)
            if unknown:
                raise RuntimeError(
                    f"--corpora has unknown values: {sorted(unknown)}. Known: {sorted(questions_by_corpus)}"
                )
            questions_by_corpus = {c: q for c, q in questions_by_corpus.items() if c in requested}
            log.info("--corpora filter: %s", sorted(questions_by_corpus))

        # Apply --models filter: scope phase 2-6 model loops to listed models. Used for
        # splitting Phase 4 across machines (32 GB Mac mini for the 6 smaller models, a
        # bigger machine for Gemma 26B + Qwen3.6 35B).
        models_filter: set[str] | None = None
        if args.models:
            models_filter = {m.strip() for m in args.models.split(",") if m.strip()}
            unknown_m = models_filter - set(cfg.ALL_MODELS)
            if unknown_m:
                raise RuntimeError(f"--models has unknown values: {sorted(unknown_m)}. Known: {sorted(cfg.ALL_MODELS)}")
            log.info("--models filter: %s", sorted(models_filter))

        # Plan units for any requested phase that doesn't already have units
        # registered in state.json. Earlier versions of this harness gated on
        # ``if not sf.units()`` (state file completely empty), which silently
        # skipped Phase 4 planning on a re-invocation that came after Phase
        # 0/1 had already populated the state file. Register additively
        # instead — never disturbs phases that already have units.
        phases = _phase_range(args.phases)
        existing_phases = {u.phase for u in sf.units()}
        missing_phases = phases - existing_phases
        if missing_phases:
            new_units = _plan_units(
                questions_by_corpus,
                missing_phases,
                args.depth,
                models_filter=models_filter,
            )
            sf.register(new_units)
            sf.save()
            log.info(
                "registered %d new units for phases %s (existing: %s)",
                len(new_units),
                sorted(missing_phases),
                sorted(existing_phases) or "none",
            )

        # Apply --rerun / --skip
        if args.rerun:
            n = sf.rerun(_parse_selectors(args.rerun))
            log.info("flipped %d units to PENDING via --rerun", n)
        if args.skip:
            n = sf.skip(_parse_selectors(args.skip))
            log.info("flipped %d units to SKIPPED via --skip", n)

        # Recover stale in-progress
        sf.recover_stale(stale_threshold_seconds=2 * args.time_limit_minutes * 60)
        sf.save()

        # Build clients
        hc = HarborClerkClient(args.api_base, verify=not args.insecure)
        if not args.dry_run:
            email = os.environ.get("HC_USERNAME")
            password = os.environ.get("HC_PASSWORD")
            if email and password:
                log.info("logging in as %s", email)
                hc.login(email, password)
            else:
                log.warning("no HC_USERNAME / HC_PASSWORD set — auth-required calls will fail")

        anthro = anthropic.Anthropic()
        judge = JudgeClient(client=anthro, model=cfg.JUDGE_MODEL)

        # CSV metrics
        metrics_path = run_dir / "metrics.csv"
        new_csv = not metrics_path.exists()
        metrics_f = metrics_path.open("a", newline="")
        metrics_writer = csv.writer(metrics_f)
        if new_csv:
            metrics_writer.writerow(
                [
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
            )

        sampler = Sampler(every_n=cfg.SAMPLE_EVERY_N)
        sweep_started = time.time()

        manifests: dict[str, CorpusManifest] = {}
        current_corpus_in_db: str | None = None
        current_model: str | None = None

        # Phase 1 MCP session — lazily opened the first time Phase 1 runs.
        # SyncMcpSession connects to Harbor Clerk's /mcp endpoint so that the
        # Sonnet 4.6 baseline generator has real KB tools available.
        mcp_session: SyncMcpSession | None = None

        # Process units in phase order
        for phase in sorted(phases):
            phase_units = [u for u in sf.units() if u.phase == phase and u.status == Status.PENDING]
            if not phase_units:
                log.info("phase %d already complete or empty", phase)
                continue
            log.info("=== phase %d: %d pending units ===", phase, len(phase_units))

            # Phase-specific setup
            if phase == 6:
                # unified: build a combined ingest dir
                unified_dir = workdir / "unified" / "ingest"
                unified_dir.mkdir(parents=True, exist_ok=True)
                for c in ("cuad", "enron", "synthetic"):
                    if c not in manifests:
                        manifests[c] = _phase0_acquire(c, workdir)
                    for f in manifests[c].ingest_dir.iterdir():
                        if f.is_file():
                            (unified_dir / f"{c}__{f.name}").write_bytes(f.read_bytes())
                unified_manifest = CorpusManifest(
                    corpus_id="unified",
                    ingest_dir=unified_dir,
                    doc_count=sum(m.doc_count for m in manifests.values()),
                    total_size_bytes=sum(m.total_size_bytes for m in manifests.values()),
                    license="various",
                    notes="unified pass",
                )
                if not args.dry_run:
                    _ingest_corpus(hc, unified_manifest)
                    current_corpus_in_db = "unified"

            for u in phase_units:
                if args.dry_run and phase > 0:
                    log.info("dry-run: skipping %s", u)
                    continue

                # Ensure correct corpus is in the DB for phases 4/5 (unified for 6)
                if phase in (4, 5) and u.corpus != current_corpus_in_db:
                    if u.corpus not in manifests:
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                    _ingest_corpus(hc, manifests[u.corpus])
                    current_corpus_in_db = u.corpus

                # Ensure correct model is active for phases 2-6
                if phase in (2, 3, 4, 5, 6) and u.model not in (None, "-", "claude-baseline"):
                    current_model = _ensure_model(hc, current_model, u.model)

                sf.set_status(u.phase, u.corpus, u.model, u.question_id, u.depth, Status.IN_PROGRESS)
                sf.save()

                t0 = time.time()
                out: dict = {}
                try:
                    if phase == 0:
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                        out = {"manifest": dataclasses.asdict(manifests[u.corpus])}
                        out["manifest"]["ingest_dir"] = str(out["manifest"]["ingest_dir"])
                    elif phase == 1:
                        # Lazily open the MCP session on first Phase 1 unit.
                        # Harbor Clerk mounts the MCP ASGI app at /mcp and
                        # FastMCP's streamable_http_app() exposes its handler
                        # at /mcp internally, so the canonical full path is
                        # /mcp/mcp. Override via HC_MCP_URL env var for
                        # non-standard deployments.
                        if mcp_session is None:
                            mcp_url = os.environ.get("HC_MCP_URL") or f"{args.api_base}/mcp/mcp"
                            log.info("opening MCP session at %s for Phase 1 baselines", mcp_url)
                            bearer = hc._client.headers.get("Authorization")
                            mcp_headers: dict[str, str] = {"Authorization": bearer} if bearer else {}
                            mcp_session = SyncMcpSession(
                                url=mcp_url,
                                headers=mcp_headers,
                            )
                        text, _lang = _question_text(questions_by_corpus[u.corpus], u.question_id)
                        out = _phase1_baseline(anthro, mcp_session, u.corpus, u.question_id, text, run_dir)
                    elif phase in (2, 3, 4, 5, 6):
                        owning_corpus = (
                            u.corpus
                            if u.corpus != "unified"
                            else _find_owning_corpus(u.question_id, questions_by_corpus)
                        )
                        text, _lang = _question_text(questions_by_corpus[owning_corpus], u.question_id)
                        out = _run_local(
                            hc=hc,
                            corpus=u.corpus,
                            model=u.model,
                            question_id=u.question_id,
                            question_text=text,
                            depth=u.depth,
                            time_limit_minutes=args.time_limit_minutes,
                            is_research=_is_research(u.question_id),
                            results_dir=run_dir,
                        )

                    # For local-model questions (phases 2-6), inspect the
                    # ResearchDetail / chat result and downgrade the unit
                    # status when the result is not a clean completion.
                    # Without this, the harness used to mark "interrupted"
                    # research tasks as DONE — leaving cells with empty
                    # answers in metrics.csv that looked like real
                    # completions.
                    final_status = Status.DONE
                    error_msg: str | None = None
                    if phase in (2, 3, 4, 5, 6):
                        result = out.get("result", {}) or {}
                        result_status = result.get("status")
                        result_answer = result.get("answer") or ""
                        if result.get("harness_aborted"):
                            final_status = Status.ERROR
                            error_msg = f"harness aborted research: {result.get('harness_abort_reason', 'unknown')}"
                        elif result_status == "completed" and result_answer:
                            final_status = Status.DONE
                        elif result_status == "completed" and not result_answer:
                            final_status = Status.DEGRADED
                            error_msg = "completed with empty answer"
                        elif result_status == "interrupted":
                            final_status = Status.ERROR
                            error_msg = "research interrupted by Harbor Clerk"
                        elif result_status in ("failed", "timeout"):
                            final_status = Status.ERROR
                            error_msg = f"research finished with status={result_status}"
                        else:
                            final_status = Status.ERROR
                            error_msg = f"unexpected result status: {result_status!r}"
                    sf.set_status(
                        u.phase,
                        u.corpus,
                        u.model,
                        u.question_id,
                        u.depth,
                        final_status,
                        error=error_msg,
                    )
                    if final_status != Status.DONE:
                        log.warning(
                            "unit %s/%s/%s ended with %s: %s",
                            u.corpus,
                            u.model,
                            u.question_id,
                            final_status.value,
                            error_msg,
                        )
                except (httpx.HTTPError, RuntimeError, KeyError) as exc:
                    log.exception("unit failed: %s", u)
                    sf.set_status(u.phase, u.corpus, u.model, u.question_id, u.depth, Status.ERROR, error=str(exc))
                finally:
                    sf.save()

                latency = time.time() - t0

                # Compute metrics for phases that produced model answers
                co = ce = eo = 0.0
                judge_verdict = ""
                judge_completeness = 0
                if phase in (4, 5):
                    baseline_path = run_dir / "baselines" / u.corpus / f"{u.question_id}.json"
                    # Cross-language ids resolve their baseline to the canonical EN id
                    if not baseline_path.exists() and "__" in u.question_id:
                        canonical = u.question_id.split("__")[0]
                        # Phase 1 always writes <id>__en.json / <id>__fr.json, never a bare <id>.json
                        baseline_path = run_dir / "baselines" / u.corpus / f"{canonical}__en.json"
                    if baseline_path.exists():
                        baseline = json.loads(baseline_path.read_text())
                        model_answer = out.get("result", {}).get("answer", "")
                        model_doc_ids = [c.get("doc_id") for c in out.get("result", {}).get("citations", [])]
                        co = citation_overlap(baseline.get("cited_doc_ids", []), model_doc_ids)
                        ce = citation_extra(baseline.get("cited_doc_ids", []), model_doc_ids)
                        eo = entity_overlap(baseline.get("answer", ""), model_answer, lang="en")

                        if phase == 5:
                            owning_c = (
                                u.corpus
                                if u.corpus != "unified"
                                else _find_owning_corpus(u.question_id, questions_by_corpus)
                            )
                            text_for_judge, _ = _question_text(questions_by_corpus[owning_c], u.question_id)
                            v = judge.judge(
                                question=text_for_judge, baseline=baseline.get("answer", ""), model_answer=model_answer
                            )
                            (run_dir / "judge" / u.corpus / u.model).mkdir(parents=True, exist_ok=True)
                            (run_dir / "judge" / u.corpus / u.model / f"{u.question_id}__{u.depth}.json").write_text(
                                json.dumps(dataclasses.asdict(v), indent=2)
                            )
                            judge_verdict = v.verdict
                            judge_completeness = v.completeness

                        sampler.note(
                            CompletionEvent(
                                phase=phase,
                                corpus=u.corpus,
                                model=u.model,
                                question_id=u.question_id,
                                baseline_answer=baseline.get("answer", "")[:200],
                                model_answer=model_answer[:200],
                                citation_overlap=co,
                                citation_extra=ce,
                                entity_overlap=eo,
                                latency_seconds=latency,
                                elapsed_total_seconds=int(time.time() - sweep_started),
                            )
                        )

                row_unit = sf.get(u.phase, u.corpus, u.model, u.question_id, u.depth)
                metrics_writer.writerow(
                    [
                        phase,
                        u.corpus,
                        u.model,
                        u.question_id,
                        u.depth,
                        row_unit.status.value if row_unit else "unknown",
                        f"{co:.3f}",
                        ce,
                        f"{eo:.3f}",
                        f"{latency:.1f}",
                        judge_verdict,
                        judge_completeness,
                    ]
                )
                metrics_f.flush()

            sampler.print_summary_table(phase=phase)

        metrics_f.close()
        log.info("sweep complete after %.1fs", time.time() - sweep_started)
        return 0
    finally:
        sf.release_lock()


if __name__ == "__main__":
    sys.exit(main())
