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
from anthropic._exceptions import OverloadedError, RateLimitError

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.corpora import cuad, enron, synthetic
from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.circuit_breaker import CircuitBreaker, is_operational_failure
from scripts.test_corpora.runner.claude_baseline import BaselineGenerator
from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession
from scripts.test_corpora.runner.judge import JudgeClient
from scripts.test_corpora.runner.metrics import citation_extra, citation_overlap, entity_overlap
from scripts.test_corpora.runner.quality import (
    baseline_quality_problem,
    classify_answer,
    find_unfilled_placeholder,
)
from scripts.test_corpora.runner.sampler import CompletionEvent, Sampler
from scripts.test_corpora.runner.state import StateFile, Status, Unit

log = logging.getLogger("sweep")


# state.json files written before the model-id alignment used informal labels
# that 404 on PUT /api/chat/models/<id>/activate. Migrate on load so a
# resumed sweep doesn't try to activate phantom models. Idempotent —
# anything not in the map is left alone.
_LEGACY_MODEL_ID_RENAMES = {
    "phi-4-mini": "phi4-mini",
    "deepseek-r1-8b": "deepseek-r1-0528-8b",
    "gemma-26b": "gemma4-26b-a4b",
    "qwen3.6-35b": "qwen36-35b-a3b",
}


# How long to wait between the original research and a one-shot retry on
# transient failure (failed / interrupted / harness_aborted). 30s lets a
# briefly wedged llama-server recover; longer doesn't help and just delays
# the eventual ERROR.
RESEARCH_RETRY_DELAY_SECONDS = 30

# When the HC API itself goes unreachable (model-switch restart, manual stop,
# etc.) we poll /api/system/health for up to this many seconds before giving
# up on the current unit. Long enough for HC's typical 6-15s restart, plus
# slack for slow llama warmup that can briefly wedge the API.
HC_RECOVERY_WAIT_SECONDS = 120

# How many sustained ConnectError outages we tolerate before bailing the
# whole sweep. With HC_RECOVERY_WAIT_SECONDS=120 this is up to ~6 minutes
# of total wait before exit — well-suited to multi-hour runs where a
# clean exit + --resume costs less than re-running cells.
HC_UNREACHABLE_CONSECUTIVE_LIMIT = 3


# Anthropic 429 (rate-limited) / 529 (overloaded) retry budget for Phase 1
# baselines. The SDK's built-in retries cover only ~5s, but a real rate-limit
# window or Anthropic overload can persist for many minutes; once that window
# is exhausted the SDK raises RateLimitError / OverloadedError and, uncaught,
# it kills the whole multi-hour sweep. We retry the baseline with exponential
# backoff, capping each sleep below the supervisor's 30-minute "stuck"
# threshold so a long backoff is never mistaken for a hang.
ANTHROPIC_RETRY_BASE_SECONDS = 60
ANTHROPIC_RETRY_MAX_DELAY_SECONDS = 600
ANTHROPIC_RETRY_BUDGET_SECONDS = 3600


def _retryable_exceptions(client_kind: str) -> tuple[type[BaseException], ...]:
    """Return the exception classes treated as transient for `client_kind`.

    Imported lazily for openai so the optional dep isn't required at module
    import time even if no openai-targeted call ever runs.
    """
    if client_kind == "anthropic":
        return (OverloadedError, RateLimitError)
    if client_kind == "openai":
        import openai

        # openai.RateLimitError covers 429; openai.APIStatusError covers other
        # transient statuses (502/503/504). We narrow inside the retry loop
        # to "truly transient" based on .status_code so a 400 bad-request
        # never gets retried.
        return (openai.RateLimitError, openai.APIStatusError)
    raise ValueError(f"unknown client_kind {client_kind!r}; supported: anthropic, openai")


def _with_provider_retry(
    fn,
    *args,
    client_kind: str = "anthropic",
    sleep=time.sleep,
    max_total_seconds: int = ANTHROPIC_RETRY_BUDGET_SECONDS,
):
    """Call ``fn(*args)``, retrying on transient API errors from `client_kind`.

    `client_kind` ∈ {"anthropic", "openai"} — picks the exception classes to
    catch. Backoff schedule is the same across providers: 60s, 120s, 240s,
    capped at 600s/attempt, with a cumulative budget of `max_total_seconds`
    (~1 hr default). On budget exhaustion the original exception is re-raised
    so the caller can leave the unit PENDING for a later --resume.

    ``sleep`` is injectable so tests need not wait in real time.
    """
    transient = _retryable_exceptions(client_kind)
    attempt = 0
    waited = 0.0
    while True:
        try:
            return fn(*args)
        except transient as exc:
            # For openai.APIStatusError, narrow to truly transient statuses;
            # a 400 bad-request shouldn't be retried.
            status = getattr(exc, "status_code", None)
            if client_kind == "openai" and status is not None and status not in (429, 500, 502, 503, 504):
                raise
            if waited >= max_total_seconds:
                raise
            delay = min(ANTHROPIC_RETRY_BASE_SECONDS * 2**attempt, ANTHROPIC_RETRY_MAX_DELAY_SECONDS)
            delay = min(delay, max_total_seconds - waited)
            log.warning(
                "%s API returned HTTP %s — backing off %.0fs before retry (attempt %d, %.0fs of %ds budget used)",
                client_kind,
                status,
                delay,
                attempt + 1,
                waited,
                max_total_seconds,
            )
            sleep(delay)
            waited += delay
            attempt += 1


def _with_anthropic_retry(fn, *args, sleep=time.sleep, max_total_seconds: int = ANTHROPIC_RETRY_BUDGET_SECONDS):
    """Back-compat alias for callers that haven't yet switched to
    _with_provider_retry. Functionally identical to
    _with_provider_retry(client_kind='anthropic', ...).
    """
    return _with_provider_retry(
        fn,
        *args,
        client_kind="anthropic",
        sleep=sleep,
        max_total_seconds=max_total_seconds,
    )


def _wait_for_hc_reachable(hc: HarborClerkClient, max_wait_seconds: int = HC_RECOVERY_WAIT_SECONDS) -> bool:
    """Poll ``/api/system/health`` until it responds or we time out.

    Returns ``True`` as soon as ``hc.health()`` returns successfully, ``False``
    if the deadline elapses with HC still unreachable. Used by the per-unit
    ConnectError handler to ride out HC restarts (e.g. menubar's restart of
    the API process during a model switch) without burning through pending
    units in machine-time.
    """
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        try:
            hc.health()
            return True
        except (httpx.HTTPError, RuntimeError):
            pass
        if time.time() < deadline:
            time.sleep(3)
    return False


def _should_judge(*, phase: int, status: Status, model_answer: str, no_judge: bool) -> bool:
    """Whether to call the LLM-as-judge for one finished unit.

    Phase 4 (main matrix) and phase 5 (parity heavies) are the phases that
    produce model answers worth judging — earlier phases either don't run
    the model (0, 1) or use cached baselines.

    We gate the call on three things beyond phase:

    1. ``--no-judge`` is the opt-out for cheap local runs (no Anthropic spend).
    2. ``final_status`` must be ``DONE``. Degraded/error units have no
       meaningful answer to compare against — judging them produces a
       junk verdict and wastes a Sonnet call.
    3. The model answer must be non-whitespace. An empty answer is the
       fingerprint of the dead-LLM cascade (chat returns "completed" with
       no tokens in ~2s) and is captured by the ``degraded`` status anyway.

    Pulled out as a helper so the gate is unit-testable and so the
    docstring lives next to the predicate.
    """
    if no_judge:
        return False
    if phase not in (4, 5):
        return False
    if status != Status.DONE:
        return False
    return bool(model_answer and model_answer.strip())


_CANARY_QUESTION = "List up to three documents you can see in the corpus and tell me their titles."


def _capture_hc_logs(run_dir: Path, log: logging.Logger) -> None:
    """Copy Harbor Clerk's server-side log files into the run directory.

    Without this, when the harness records ``"research interrupted by Harbor
    Clerk"`` there's no easy way to tell *why* — the only log written into
    the run directory is the harness side. HC's own log (api.log,
    worker-*.log, postgres.log, llm.log, etc.) lives in
    ``~/Library/Application Support/Harbor Clerk/logs/`` on macOS native.

    Honours the ``HC_LOG_DIR`` env var. Best-effort: a missing source
    directory or unreadable file is logged at warning level and skipped,
    never raised — the sweep must not fail at finalize because logs
    couldn't be copied.

    Called once at sweep finalize. Captures a snapshot, not a tail —
    operators interpret the timeline by cross-referencing with the
    harness ``log.txt`` and ``metrics.csv`` timestamps.
    """
    import shutil

    default_log_dir = Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "logs"
    src_dir = Path(os.environ.get("HC_LOG_DIR") or default_log_dir)
    if not src_dir.exists():
        log.info("HC log capture skipped: source dir %s does not exist", src_dir)
        return

    dst_dir = run_dir / "hc_logs"
    dst_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    for src in src_dir.glob("*.log"):
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, dst_dir / src.name)
            copied += 1
        except OSError as exc:
            log.warning("HC log capture: failed to copy %s: %r", src, exc)
            skipped += 1

    log.info(
        "HC log capture: copied %d files from %s to %s (skipped %d)",
        copied,
        src_dir,
        dst_dir,
        skipped,
    )


def _run_canary(hc: HarborClerkClient, corpus: str, log: logging.Logger) -> dict[str, object]:
    """Send one trivial chat question to verify HC + the active LLM can produce
    a non-empty, non-refusal response against the just-ingested corpus.

    Used as a pre-flight diagnostic before the per-unit loop chews through
    20+ units against a structurally broken HC (the synthetic-block 100%
    failure pattern in 2026-05-05-prod: 280 docs ingested, model_status
    ready, every chat returns empty in 2s). The canary catches that state
    in ~3s instead of burning hours of sweep time.

    Returns a result dict with keys:
      - ``passed`` (bool): True if the answer classified as "real"
      - ``label`` (str): classify_answer's label (real/refusal/roleplay/empty)
      - ``elapsed_seconds`` (float): wall clock of the chat call
      - ``answer_chars`` (int): length of the streamed answer
      - ``reason`` (str | None): the reason from classify_answer when failed

    This function NEVER raises; on exception the result records ``passed=False``
    with a stringified error in ``reason``. The canary must not be a source
    of new failure modes.
    """
    t0 = time.time()
    try:
        conv_id = hc.create_conversation(title=f"test-corpora/canary/{corpus}")
        events = list(hc.stream_ask(conv_id, _CANARY_QUESTION))
    except Exception as exc:
        log.warning("canary failed for corpus=%s: exception during chat: %r", corpus, exc)
        return {
            "passed": False,
            "label": "exception",
            "elapsed_seconds": time.time() - t0,
            "answer_chars": 0,
            "reason": f"exception: {exc!r}",
        }

    final_text = "".join(e.get("content", "") for e in events if e.get("type") == "token")
    elapsed = time.time() - t0
    label, reason = classify_answer(final_text)
    passed = label == "real"

    if passed:
        log.info(
            "canary OK for corpus=%s: %d chars in %.1fs",
            corpus,
            len(final_text),
            elapsed,
        )
    else:
        log.warning(
            "canary failed for corpus=%s: label=%s reason=%s (elapsed %.1fs, %d chars)",
            corpus,
            label,
            reason,
            elapsed,
            len(final_text),
        )

    return {
        "passed": passed,
        "label": label,
        "elapsed_seconds": elapsed,
        "answer_chars": len(final_text),
        "reason": reason,
    }


def _is_retryable_research_failure(out: dict) -> bool:
    """Whether ``out["result"]`` warrants a one-shot retry.

    Retryable:
      - ``status == "failed"`` — HC's research raised an error mid-stream.
        Most often a llama-server hiccup, sometimes a model that wasn't
        actually warm yet despite ``model_status == "ready"``.
      - ``status == "interrupted"`` — HC's SSE stream closed before
        completion. Could be a real disconnect or a watchdog catch.
      - ``harness_aborted`` — our watchdog forced the stream closed
        because the model went unhealthy. The retry covers the case
        where the model recovers (auto-restart, transient glitch).

    Not retryable: ``status == "completed"`` (even with an empty answer —
    same query against same model won't suddenly succeed), or non-research
    output (chat questions don't go through this path).
    """
    result = out.get("result", {}) or {}
    if result.get("harness_aborted"):
        return True
    return result.get("status") in ("failed", "interrupted")


# ── argparse ──


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sweep")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workdir", default=str(cfg.WORKDIR_DEFAULT))
    p.add_argument("--api-base", default=cfg.API_BASE)
    p.add_argument(
        "--mode",
        choices=["retrieval-eval", "answer-eval"],
        default=None,
        help=(
            "alternate fast-path mode. With --mode retrieval-eval, the 6-phase "
            "sweep is bypassed and a retrieval-only evaluation is run against "
            "existing baselines (recall@K, MRR, nDCG@10). No LLM is invoked. "
            "With --mode answer-eval, a model is run through HC's MCP over a "
            "ground-truth set and its answers are scored for correctness, "
            "groundedness, and completeness against expert labels. Both modes "
            "require --label; see `retrieval_eval.py` / `answer_eval.py` for "
            "the full flag list."
        ),
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="acknowledge that state.json already exists and continue from it. "
        "Required when state.json exists; harmless when it doesn't. Without "
        "this flag, an existing state file fails fast so a re-used --run-id "
        "doesn't silently inherit a prior run's units.",
    )
    p.add_argument("--rerun", default="")
    p.add_argument("--skip", default="")
    p.add_argument("--phases", default="0-6")
    p.add_argument("--models", default="")
    p.add_argument("--corpora", default="")
    p.add_argument("--depth", default=cfg.DEFAULT_DEPTH)
    p.add_argument("--time-limit-minutes", type=int, default=30)
    p.add_argument("--insecure", action="store_true", help="disable TLS verify (self-signed)")
    p.add_argument("--dry-run", action="store_true", help="run Phase 0 only, no API calls beyond acquire")
    p.add_argument(
        "--no-judge",
        action="store_true",
        help=(
            "skip the LLM-as-judge call in phases 4 and 5. Use for cheap local-only "
            "runs when you don't want to spend Anthropic credits on Sonnet judgments."
        ),
    )
    p.add_argument(
        "--skip-canary",
        action="store_true",
        help=(
            "skip the per-corpus canary pre-flight chat. The canary catches the "
            "structurally-broken-corpus pattern (all units degrade in 2s) in ~3s "
            "instead of letting the circuit breaker chew through 9+ units first. "
            "Pass this flag to disable on local runs where the diagnostic noise "
            "isn't worth the extra chat call."
        ),
    )
    p.add_argument(
        "--no-hc-logs",
        action="store_true",
        help=(
            "skip the HC log capture at sweep finalize. By default the harness "
            "copies $HC_LOG_DIR (or ~/Library/Application Support/Harbor Clerk/"
            "logs on macOS) into <run_dir>/hc_logs/ so post-hoc analysis can "
            "cross-reference HC's own logs against metrics.csv timestamps. "
            "Pass this flag on machines where the path doesn't apply."
        ),
    )
    p.add_argument(
        "--no-ingest",
        action="store_true",
        help=(
            "skip all corpus ingest; baseline/run against whatever is already loaded in HC. "
            "For eval-fixture baseline capture against a pre-loaded unified corpus."
        ),
    )
    # Attach retrieval-eval flags. They're only meaningful when --mode
    # retrieval-eval is set, but registering them on the main parser keeps
    # --help discoverable.
    from scripts.test_corpora.runner import answer_eval, retrieval_eval

    retrieval_eval.add_cli_args(p)
    answer_eval.add_cli_args(p)
    return p


def _parse_selectors(s: str) -> dict[str, list[str]]:
    """Parse the CLI selector string into a multimap.

    ``id=A,id=B,phase=4`` → ``{"id": ["A", "B"], "phase": ["4"]}``.

    Repeated keys accumulate into a list (OR within key). Different keys are
    AND'd downstream in ``StateFile.rerun`` / ``skip``. The previous
    ``dict(...)`` implementation silently collapsed repeated keys to the last
    value, so ``--rerun 'id=A,id=B,id=C'`` matched only ``C``.
    """
    if not s:
        return {}
    result: dict[str, list[str]] = {}
    for part in s.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        result.setdefault(k, []).append(v)
    return result


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
            # smoke — one model, one corpus. Skipped if cuad or qwen36-35b-a3b isn't in scope.
            if "cuad" in questions_by_corpus and (models_filter is None or "qwen36-35b-a3b" in models_filter):
                units.append(
                    Unit(phase=2, corpus="cuad", model="qwen36-35b-a3b", question_id="cuad-research-1", depth=depth)
                )
        elif phase == 3:
            # depth coverage — same model × all three depths on cuad. Skipped if cuad or
            # qwen36-35b-a3b isn't in scope.
            if "cuad" in questions_by_corpus and (models_filter is None or "qwen36-35b-a3b" in models_filter):
                for d in cfg.DEPTHS:
                    for q in _question_ids(questions_by_corpus["cuad"]):
                        units.append(Unit(phase=3, corpus="cuad", model="qwen36-35b-a3b", question_id=q, depth=d))
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
        conv_id = hc.create_conversation(title=f"test-corpora/{corpus}/{model}/{question_id}")
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
        # Preserve the raw SSE event stream so post-hoc analysis can tell
        # ``model invoked the tool`` from ``model narrated a tool call as
        # text.`` ``tool_call`` and ``tool_result`` events carry the
        # function name + arguments + raw result; ``tool_call_count`` is
        # the cheap summary the matrix can group by.
        tool_events = [e for e in events if e.get("type") in ("tool_call", "tool_result")]
        tool_call_count = sum(1 for e in events if e.get("type") == "tool_call")
        result = {
            "status": "completed",
            "answer": final_text,
            "citations": citations,
            "conversation_id": conv_id,
            "tool_events": tool_events,
            "tool_call_count": tool_call_count,
        }

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


def _hc_corpus_matches(hc: HarborClerkClient, manifest: CorpusManifest) -> bool:
    """Return True iff HC already has the given corpus loaded.

    Two checks: a watch folder whose ``path`` equals ``manifest.ingest_dir``,
    AND ``document_count() >= 50% of manifest.doc_count``. The doc-count
    floor matches ``_ingest_corpus``'s "ingest looks incomplete" sanity
    check, so anything we'd accept fresh we also accept on resume.

    Pure point-in-time check — the caller is responsible for ensuring HC's
    queue is drained before trusting the doc count (see ``_can_skip_ingest``).
    """
    target = str(manifest.ingest_dir)
    folders = hc.watch_folder_list()
    if not any(f.get("path") == target for f in folders):
        return False
    threshold = max(1, int(manifest.doc_count * 0.5))
    return hc.document_count() >= threshold


def _can_skip_ingest(
    hc: HarborClerkClient,
    manifest: CorpusManifest,
    max_drain_wait: int = 4 * 3600,
) -> bool:
    """Decide whether to skip ``_ingest_corpus``'s wipe-and-re-ingest cycle.

    Used at sweep startup, before the first per-unit ingest decision in
    phases 4/5/6. If a previous sweep died mid-ingest, HC may still have
    pending jobs in the queue — trusting ``document_count`` while the
    queue is draining could let research start against a partial corpus.
    So we drain first (up to ``max_drain_wait``), then ask whether HC's
    state matches the manifest.
    """
    if not hc.pipeline_quiet():
        log.info("pipeline still draining at startup — waiting before checking corpus state")
        if not hc.wait_for_quiet_pipeline(max_wait_seconds=max_drain_wait):
            return False
    return _hc_corpus_matches(hc, manifest)


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

    # --mode retrieval-eval is a separate fast path: no state.json, no per-phase
    # planning, no model switching, no LLM. Dispatch and return before the
    # main sweep loop spins up clients or touches state.
    if args.mode == "retrieval-eval":
        from scripts.test_corpora.runner import retrieval_eval

        return retrieval_eval.main_from_args(args)

    if args.mode == "answer-eval":
        from scripts.test_corpora.runner import answer_eval

        return answer_eval.main_from_args(args)

    state_path = run_dir / "state.json"
    sf = StateFile(state_path)
    sf.acquire_lock()
    try:
        sf.load()

        # Guard against accidentally re-using a --run-id from a prior run.
        # Without --resume, an existing state.json fails fast — otherwise the
        # sweep would silently inherit whatever was there.
        if sf.units() and not args.resume:
            raise SystemExit(
                f"state.json at {state_path} already has {len(sf.units())} units. "
                "To proceed, either:\n"
                "  - add --resume to continue from this state (combine with "
                "--rerun '<selectors>' or --skip '<selectors>' to adjust which "
                "cells run)\n"
                "  - or pick a fresh --run-id to start a new run"
            )

        # Recover units left IN_PROGRESS by a previous crashed run.
        # We hold the state lock now, so any unit still IN_PROGRESS is by
        # definition orphaned (no other writer can have it). Flip them back
        # to PENDING so --resume picks them up. Threshold=0 → "anything
        # whose heartbeat is older than now", which is everything still on
        # disk by the time we reach this point.
        n_recovered = sf.recover_stale(stale_threshold_seconds=0)
        if n_recovered:
            sf.save()
            log.warning("recovered %d unit(s) left IN_PROGRESS by a prior crashed run", n_recovered)

        # Migrate legacy model ids in state.json before any other logic
        # consults sf.units(). Without this, --resume on a state file
        # written before PR #300 keeps trying to activate the old labels
        # (e.g. gemma-26b) which 404 against HC's actual registry.
        n_renamed = sf.rename_model_ids(_LEGACY_MODEL_ID_RENAMES)
        if n_renamed:
            sf.save()
            log.warning("migrated %d legacy model ids in state.json", n_renamed)

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

        # Per-model circuit breaker. Caps the damage from a dead-LLM cascade
        # by sleeping after N consecutive operational failures, and skips
        # remaining units for a model that's tripped too many times.
        # State persists across the whole sweep (multiple corpora, multiple
        # phases) since a model that's broken in one block typically stays
        # broken until manual intervention.
        breaker = CircuitBreaker()

        # Per-corpus canary outcomes. Run once per corpus before the first
        # phase-2-6 unit, write to ``canary.json`` in the run dir for
        # post-hoc analysis. Advisory only — the circuit breaker handles
        # cascade prevention; this just gives operators a heads-up when a
        # whole corpus block is structurally broken.
        canary_results: dict[str, dict[str, object]] = {}

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

        # HC-unreachability tolerance. When the sweep is running and HC
        # restarts (e.g. menubar's granular restart on model switch), every
        # in-flight request gets ConnectError until HC comes back — typically
        # 6-15s of downtime. Per-unit ConnectError handler waits up to
        # HC_RECOVERY_WAIT_SECONDS for HC to come back before counting the
        # outage; only HC_UNREACHABLE_CONSECUTIVE_LIMIT sustained timeouts
        # back-to-back trigger a clean exit. The unit is always flipped
        # PENDING (not ERROR) so a `--resume` picks up exactly where we left
        # off, no `--rerun status=error` needed.
        hc_connect_errors = 0

        # Phase 1 MCP session — lazily opened the first time Phase 1 runs.
        # SyncMcpSession connects to Harbor Clerk's /mcp endpoint so that the
        # Sonnet 4.6 baseline generator has real KB tools available.
        mcp_session: SyncMcpSession | None = None

        # Iterate corpus-outer / phase-inner so each corpus is ingested at most
        # once per sweep run. The previous phase-outer / corpus-inner ordering
        # forced the corpus to be wiped + re-ingested between phases (e.g.
        # cuad ingested once for Phase 1 baselines, then again for Phase 4
        # research, then again for Phase 5 parity) — 10 ingests across a full
        # sweep, with each ~5-15 min on a Mac mini. Corpus-outer collapses
        # that to 4 ingests (cuad + enron + synthetic + unified).
        #
        # Within a corpus block, units are sorted by (phase, model, question_id)
        # so Phase 0 acquire runs before Phase 1 ingest, baselines before
        # research, etc. The unified Phase 6 is special-cased to run LAST
        # because its ingest dir has to be built from the other three corpora
        # first.
        by_corpus: dict[str, list[Unit]] = {}
        for u in sf.units():
            if u.status != Status.PENDING:
                continue
            if u.phase not in phases:
                continue
            by_corpus.setdefault(u.corpus, []).append(u)
        for units_in in by_corpus.values():
            units_in.sort(key=lambda u: (u.phase, u.model, u.question_id))
        # Process non-unified corpora first, in stable order, then unified last.
        ordered_corpora = sorted(c for c in by_corpus if c != "unified")
        if "unified" in by_corpus:
            ordered_corpora.append("unified")

        if not ordered_corpora:
            log.info("no pending units in scope")

        for corpus in ordered_corpora:
            corpus_units = by_corpus[corpus]
            phases_in_block = sorted(set(u.phase for u in corpus_units))
            log.info(
                "=== corpus %s: %d pending units (phases %s) ===",
                corpus,
                len(corpus_units),
                phases_in_block,
            )

            # Special setup for the unified corpus: build the combined ingest
            # dir from cuad+enron+synthetic, then ingest. The non-unified
            # ingest gate inside the per-unit body wouldn't trigger because
            # phase 6 isn't in (1, 4, 5).
            if corpus == "unified" and not args.dry_run and not args.no_ingest:
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
                    doc_count=sum(m.doc_count for m in manifests.values() if m.corpus_id != "unified"),
                    total_size_bytes=sum(m.total_size_bytes for m in manifests.values() if m.corpus_id != "unified"),
                    license="various",
                    notes="unified pass",
                )
                manifests["unified"] = unified_manifest
                if current_corpus_in_db is None and _can_skip_ingest(hc, unified_manifest):
                    log.info("HC already has unified corpus loaded — skipping re-ingest")
                    current_corpus_in_db = "unified"
                elif current_corpus_in_db != "unified":
                    _ingest_corpus(hc, unified_manifest)
                    current_corpus_in_db = "unified"
            elif corpus == "unified" and args.no_ingest:
                current_corpus_in_db = "unified"

            last_phase_logged: int | None = None
            for u in corpus_units:
                # Phase boundary log within a corpus block, helps eyeball
                # progress when a corpus contains units across multiple phases.
                if u.phase != last_phase_logged:
                    log.info("--- phase %d for %s ---", u.phase, corpus)
                    last_phase_logged = u.phase
                phase = u.phase  # rest of the body uses `phase` as a free var
                if args.dry_run and phase > 0:
                    log.info("dry-run: skipping %s", u)
                    continue

                # Circuit-breaker gate. Only meaningful for unit phases that
                # actually run the local model (2-6). Phase 0/1 have their
                # own failure modes (ingest, baseline-gen) that don't fit
                # the per-model breaker pattern. Skipped units stay PENDING
                # so ``--resume`` after manual intervention picks them up.
                if phase in (2, 3, 4, 5, 6):
                    if breaker.should_skip(u.model):
                        log.warning(
                            "skipping %s/%s/%s — circuit breaker is open (left PENDING for --resume)",
                            u.corpus,
                            u.model,
                            u.question_id,
                        )
                        continue
                    if breaker.should_pause(u.model):
                        breaker.pause_and_reset(u.model, log)

                # Ensure correct corpus is in the DB for phases 1/4/5 (unified for 6).
                # Phase 1 baselines call HC's MCP for KB tools — Sonnet's queries hit
                # whatever is currently in HC's DB, so a fresh sweep that doesn't
                # ingest first generates baselines against the wrong corpus (or no
                # corpus at all). On a fresh process (current_corpus_in_db is None)
                # we first ask HC whether it already has u.corpus loaded AND its
                # queue has drained — if so, skip the wipe so `--resume` after a
                # mid-corpus crash doesn't lose the existing ingest.
                if phase in (1, 4, 5) and u.corpus != current_corpus_in_db and not args.no_ingest:
                    if u.corpus not in manifests:
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                    # Belt-and-suspenders: if Phase 0's ingest dir got cleaned up
                    # between runs (manual cleanup, fs eviction, lost mount), the
                    # ingest below would hit the 120s "watcher never enqueued any
                    # jobs" timeout. Re-acquire on the spot so the user doesn't
                    # have to chase a --rerun 'phase=0,corpus=...' workaround.
                    ingest_dir = manifests[u.corpus].ingest_dir
                    if not ingest_dir.exists() or not any(ingest_dir.iterdir()):
                        log.warning(
                            "ingest dir for %s is missing or empty (%s) — re-acquiring",
                            u.corpus,
                            ingest_dir,
                        )
                        manifests[u.corpus] = _phase0_acquire(u.corpus, workdir)
                    if current_corpus_in_db is None and _can_skip_ingest(hc, manifests[u.corpus]):
                        log.info("HC already has %s loaded — skipping re-ingest", u.corpus)
                        current_corpus_in_db = u.corpus
                    elif u.corpus != current_corpus_in_db:
                        _ingest_corpus(hc, manifests[u.corpus])
                        current_corpus_in_db = u.corpus
                elif phase in (1, 4, 5) and u.corpus != current_corpus_in_db and args.no_ingest:
                    current_corpus_in_db = u.corpus

                # Ensure correct model is active for phases 2-6
                if phase in (2, 3, 4, 5, 6) and u.model not in (None, "-", "claude-baseline"):
                    current_model = _ensure_model(hc, current_model, u.model)

                # Per-corpus canary. Runs once per corpus the first time we
                # hit a phase-2-6 unit for it, AFTER both corpus ingest and
                # model activation — otherwise the canary tests against
                # whatever HC happened to have loaded (often nothing on a
                # fresh run), producing a misleading "empty" diagnostic
                # unrelated to the structurally-broken-corpus pattern the
                # canary is meant to catch. Advisory only: the circuit
                # breaker handles cascade prevention; this just gives
                # operators an early signal. ``--skip-canary`` opts out.
                if phase in (2, 3, 4, 5, 6) and not args.skip_canary and u.corpus not in canary_results:
                    canary_results[u.corpus] = _run_canary(hc, u.corpus, log)
                    # Persist after each canary so a crash mid-sweep doesn't
                    # lose the diagnostic.
                    (run_dir / "canary.json").write_text(json.dumps(canary_results, indent=2))

                sf.set_status(u.phase, u.corpus, u.model, u.question_id, u.depth, Status.IN_PROGRESS)
                sf.save()

                # Per-unit progress marker. The harness can sit quiet for
                # minutes between MCP/auth pings while a research task runs,
                # making it hard to tell from the log what's actually in
                # flight. This line (paired with the "Finished" line at the
                # end of the loop body) brackets each unit explicitly.
                log.info(
                    "Starting %s/%s [%s] (phase %d, depth %s)",
                    u.corpus,
                    u.question_id,
                    u.model,
                    u.phase,
                    u.depth,
                )

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
                            # Forward HC's bearer to the separate MCP httpx client.
                            # After PR #306 the token lives inside hc._client.auth, not
                            # in client.headers — `get_bearer_token` triggers the lazy
                            # login if needed and returns the active token. Without
                            # this, the MCP session goes out unauthenticated and 401s.
                            token = hc.get_bearer_token()
                            mcp_headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
                            mcp_session = SyncMcpSession(
                                url=mcp_url,
                                headers=mcp_headers,
                            )
                        text, _lang = _question_text(questions_by_corpus[u.corpus], u.question_id)
                        placeholder = find_unfilled_placeholder(text)
                        if placeholder:
                            # The question template still contains a
                            # ``{{contract_a}}``-style marker. Sampling should
                            # have substituted it before the unit was planned;
                            # sending it verbatim wastes a Claude baseline call
                            # and produces a "please clarify" answer that taints
                            # every downstream model run for the same question.
                            log.error(
                                "phase 1 baseline aborted for %s/%s: unfilled placeholder %s",
                                u.corpus,
                                u.question_id,
                                placeholder,
                            )
                            sf.set_status(
                                u.phase,
                                u.corpus,
                                u.model,
                                u.question_id,
                                u.depth,
                                Status.ERROR,
                                error=f"unfilled placeholder: {placeholder}",
                            )
                            continue
                        out = _with_anthropic_retry(
                            _phase1_baseline, anthro, mcp_session, u.corpus, u.question_id, text, run_dir
                        )
                    elif phase in (2, 3, 4, 5, 6):
                        owning_corpus = (
                            u.corpus
                            if u.corpus != "unified"
                            else _find_owning_corpus(u.question_id, questions_by_corpus)
                        )
                        text, _lang = _question_text(questions_by_corpus[owning_corpus], u.question_id)
                        placeholder = find_unfilled_placeholder(text)
                        if placeholder:
                            # Same reasoning as phase 1: don't ship a question
                            # with an unfilled template marker. Mark ERROR so
                            # the unit shows up in the matrix with a clear
                            # reason rather than a 422 or a "please clarify"
                            # answer that looks like an LLM failure.
                            log.error(
                                "phase %d aborted for %s/%s/%s: unfilled placeholder %s",
                                phase,
                                u.corpus,
                                u.model,
                                u.question_id,
                                placeholder,
                            )
                            sf.set_status(
                                u.phase,
                                u.corpus,
                                u.model,
                                u.question_id,
                                u.depth,
                                Status.ERROR,
                                error=f"unfilled placeholder: {placeholder}",
                            )
                            continue
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

                        # One-shot retry on transient research failures. The most
                        # common cause is the model wasn't actually warm despite
                        # HC reporting state=ready; second-most-common is
                        # llama-server hiccup mid-run that recovered. Wait, verify
                        # the model is still ready, then re-run the same question
                        # at the same depth. If the retry also fails the unit gets
                        # the normal status downgrade (ERROR) below.
                        if _is_research(u.question_id) and _is_retryable_research_failure(out):
                            res = out.get("result", {}) or {}
                            log.warning(
                                "research %s/%s/%s failed (status=%s, harness_aborted=%s); retrying once after %ds",
                                u.corpus,
                                u.model,
                                u.question_id,
                                res.get("status"),
                                bool(res.get("harness_aborted")),
                                RESEARCH_RETRY_DELAY_SECONDS,
                            )
                            time.sleep(RESEARCH_RETRY_DELAY_SECONDS)
                            if not hc.wait_for_model_ready(u.model, max_wait_seconds=600):
                                log.warning("model %s not ready before retry — accepting failure", u.model)
                            else:
                                out = _run_local(
                                    hc=hc,
                                    corpus=u.corpus,
                                    model=u.model,
                                    question_id=u.question_id,
                                    question_text=text,
                                    depth=u.depth,
                                    time_limit_minutes=args.time_limit_minutes,
                                    is_research=True,
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
                        elif result_status == "completed":
                            # Tighter DONE predicate: a non-empty answer is not
                            # automatically a real engagement with the corpus.
                            # ``classify_answer`` is the single authority on
                            # completed-status outcomes — it folds the
                            # "empty/whitespace answer" case in alongside
                            # refusals ("I don't have the capability...") and
                            # roleplay (small models that emit ``[search_documents:
                            # "..."]`` as text instead of invoking the tool). Both
                            # the chat path and the research path get the same
                            # treatment so the matrix consistently captures
                            # "answer said something useful" vs "answer punted."
                            label, reason = classify_answer(result_answer)
                            if label == "real":
                                final_status = Status.DONE
                            else:
                                final_status = Status.DEGRADED
                                error_msg = reason
                        elif result_status == "interrupted":
                            final_status = Status.ERROR
                            # ``result["error"]`` carries the structured reason
                            # HC set on the ResearchState row (synthesis HTTP
                            # error, reaper timeout, stream disconnect). Falls
                            # back to the generic label only when the field is
                            # absent — older snapshots before the schema field
                            # was added still parse cleanly.
                            hc_error = result.get("error")
                            error_msg = (
                                f"research interrupted by Harbor Clerk: {hc_error}"
                                if hc_error
                                else "research interrupted by Harbor Clerk"
                            )
                        elif result_status in ("failed", "timeout"):
                            final_status = Status.ERROR
                            hc_error = result.get("error")
                            error_msg = (
                                f"research finished with status={result_status}: {hc_error}"
                                if hc_error
                                else f"research finished with status={result_status}"
                            )
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

                    # Feed the circuit breaker. Only model-running phases
                    # (2-6) count toward the per-model counter; phase 0/1
                    # failures aren't symptoms of the model under test
                    # being unhealthy.
                    if phase in (2, 3, 4, 5, 6):
                        op_fault = final_status != Status.DONE and is_operational_failure(error_msg)
                        breaker.note(u.model, operational_failure=op_fault)
                except httpx.ConnectError as exc:
                    # HC is refusing connections — likely restarting (e.g.
                    # menubar restarts the API process during a model switch).
                    # Mark this unit PENDING (it didn't fail, HC was unreachable)
                    # so --resume picks it up, then actively wait for HC to
                    # come back rather than burning through pending units in
                    # machine-time.
                    log.warning(
                        "ConnectError on unit %s/%s/%s — HC at %s may be restarting; "
                        "waiting up to %ds for it to come back",
                        u.corpus,
                        u.model,
                        u.question_id,
                        args.api_base,
                        HC_RECOVERY_WAIT_SECONDS,
                    )
                    cur = sf.get(u.phase, u.corpus, u.model, u.question_id, u.depth)
                    if cur is not None:
                        cur.status = Status.PENDING
                        cur.heartbeat = None
                        cur.started_at = None
                        cur.error = None
                    sf.save()

                    if _wait_for_hc_reachable(hc, max_wait_seconds=HC_RECOVERY_WAIT_SECONDS):
                        log.info("HC reachable again; resetting outage counter and continuing")
                        hc_connect_errors = 0
                    else:
                        hc_connect_errors += 1
                        log.warning(
                            "HC still unreachable after %ds (%d/%d sustained outages)",
                            HC_RECOVERY_WAIT_SECONDS,
                            hc_connect_errors,
                            HC_UNREACHABLE_CONSECUTIVE_LIMIT,
                        )
                        if hc_connect_errors >= HC_UNREACHABLE_CONSECUTIVE_LIMIT:
                            log.error(
                                "HC API at %s never came back after %d × %ds attempts; "
                                "exiting cleanly. Restart HC and re-run with --resume.",
                                args.api_base,
                                hc_connect_errors,
                                HC_RECOVERY_WAIT_SECONDS,
                            )
                            raise SystemExit(
                                f"HC API at {args.api_base} unreachable for "
                                f"{hc_connect_errors} sustained outages "
                                f"({HC_RECOVERY_WAIT_SECONDS}s each); exiting cleanly so "
                                "--resume picks up where we left off."
                            ) from exc
                except (OverloadedError, RateLimitError):
                    # Anthropic stayed rate-limited (429) or overloaded (529)
                    # past the ~1 hr retry budget. Not a unit-level failure —
                    # leave it PENDING (not ERROR) so `--resume` re-runs it
                    # once Anthropic recovers, and continue so the rest of the
                    # corpus still finishes.
                    log.warning(
                        "unit %s/%s/%s: Anthropic rate-limited/overloaded past the %ds retry budget; "
                        "leaving PENDING for --resume and continuing",
                        u.corpus,
                        u.model,
                        u.question_id,
                        ANTHROPIC_RETRY_BUDGET_SECONDS,
                    )
                    cur = sf.get(u.phase, u.corpus, u.model, u.question_id, u.depth)
                    if cur is not None:
                        cur.status = Status.PENDING
                        cur.heartbeat = None
                        cur.started_at = None
                        cur.error = None
                    sf.save()
                except (httpx.HTTPError, RuntimeError, KeyError) as exc:
                    log.exception("unit failed: %s", u)
                    sf.set_status(u.phase, u.corpus, u.model, u.question_id, u.depth, Status.ERROR, error=str(exc))
                    # Non-connect error: HC is reachable, just this unit failed.
                    hc_connect_errors = 0
                else:
                    # Successful unit (no exception in the try). Reset the
                    # outage counter; we just talked to HC, it's up.
                    hc_connect_errors = 0
                finally:
                    # If the unit is still IN_PROGRESS — meaning an uncaught
                    # exception (KeyboardInterrupt, tenacity.RetryError, etc.)
                    # is propagating out — flip it back to PENDING so a
                    # `--resume` invocation picks it up cleanly without
                    # needing `--rerun`.
                    current = sf.get(u.phase, u.corpus, u.model, u.question_id, u.depth)
                    if current is not None and current.status == Status.IN_PROGRESS:
                        log.warning(
                            "unit %s/%s/%s left IN_PROGRESS by uncaught exception; flipping to PENDING for --resume",
                            u.corpus,
                            u.model,
                            u.question_id,
                        )
                        current.status = Status.PENDING
                        current.heartbeat = None
                        current.started_at = None
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
                        # Refuse to grade against a baseline that's itself
                        # unusable — Claude saw an empty corpus, an unfilled
                        # placeholder, or punted with a refusal. Computing
                        # citation_overlap or entity_overlap against such an
                        # answer is noise: the row still gets written so the
                        # operational status is preserved, but the quality
                        # columns stay at their zero defaults and we don't
                        # spend Sonnet credits judging it.
                        baseline_problem = baseline_quality_problem(baseline)
                        if baseline_problem:
                            log.warning(
                                "skipping metrics for %s/%s/%s: %s",
                                u.corpus,
                                u.model,
                                u.question_id,
                                baseline_problem,
                            )
                        else:
                            model_doc_ids = [c.get("doc_id") for c in out.get("result", {}).get("citations", [])]
                            co = citation_overlap(baseline.get("cited_doc_ids", []), model_doc_ids)
                            ce = citation_extra(baseline.get("cited_doc_ids", []), model_doc_ids)
                            eo = entity_overlap(baseline.get("answer", ""), model_answer, lang="en")

                            # Run LLM-as-judge when ``_should_judge`` says so. Phase 4
                            # (main matrix) and phase 5 (parity heavies) qualify;
                            # earlier phases don't produce a model answer worth
                            # comparing against the baseline. Nested under the
                            # ``baseline_problem is None`` branch so we don't burn
                            # Sonnet credits judging against a baseline we already
                            # know is corrupt.
                            if _should_judge(
                                phase=phase,
                                status=final_status,
                                model_answer=model_answer,
                                no_judge=args.no_judge,
                            ):
                                owning_c = (
                                    u.corpus
                                    if u.corpus != "unified"
                                    else _find_owning_corpus(u.question_id, questions_by_corpus)
                                )
                                text_for_judge, _ = _question_text(questions_by_corpus[owning_c], u.question_id)
                                try:
                                    v = judge.judge(
                                        question=text_for_judge,
                                        baseline=baseline.get("answer", ""),
                                        model_answer=model_answer,
                                    )
                                except Exception:
                                    # Judge failures must not poison the sweep — log
                                    # and carry on with empty verdict columns.
                                    log.warning(
                                        "judge call failed for %s/%s/%s; continuing without verdict",
                                        u.corpus,
                                        u.model,
                                        u.question_id,
                                        exc_info=True,
                                    )
                                else:
                                    (run_dir / "judge" / u.corpus / u.model).mkdir(parents=True, exist_ok=True)
                                    (
                                        run_dir / "judge" / u.corpus / u.model / f"{u.question_id}__{u.depth}.json"
                                    ).write_text(json.dumps(dataclasses.asdict(v), indent=2))
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

                # Closing marker for this unit — pairs with the "Starting"
                # line above so each unit is a clearly delimited block in
                # the log.
                log.info(
                    "Finished %s/%s [%s] — %s in %.1fs",
                    u.corpus,
                    u.question_id,
                    u.model,
                    row_unit.status.value if row_unit else "unknown",
                    latency,
                )

            sampler.print_summary_table(phase=phase)

        metrics_f.close()
        if not args.no_hc_logs:
            _capture_hc_logs(run_dir, log)
        log.info("sweep complete after %.1fs", time.time() - sweep_started)
        return 0
    finally:
        sf.release_lock()


if __name__ == "__main__":
    sys.exit(main())
