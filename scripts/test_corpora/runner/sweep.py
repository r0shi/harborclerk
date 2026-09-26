"""Six-phase test sweep entrypoint.

Reads/writes ``state.json`` in the run-id directory. Schedules the next
pending unit, dispatches to the right handler, updates state, repeats.

Phases:
  0. acquire   — download / generate the three corpora
  1. baseline  — Sonnet 4.6 + Harbor Clerk MCP, save baselines/<corpus>/<q>.json
  2. smoke     — one large model × one corpus, iterate on bugs
  3. depth     — same model × all three depths
  4. models    — all 8 × all 3 corpora × standard, completion-only
  5. parity    — top 2 × all 3, mechanical + judge
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
import platform
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
from scripts.test_corpora.runner import answer_key, spend
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

        # openai.RateLimitError is a SUBCLASS of openai.APIStatusError, so
        # catching APIStatusError alone covers both. We narrow inside the
        # retry loop to "truly transient" based on .status_code so a 400
        # bad-request never gets retried — RateLimitError (429) is always in
        # the allowlist, but to make the intent explicit and survive future
        # filter tightening, the retry loop checks isinstance() first.
        return (openai.APIStatusError,)
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
            # a 400 bad-request shouldn't be retried. RateLimitError is a
            # subclass of APIStatusError — always retry it regardless of the
            # status-code filter (defensive against the filter shifting).
            status = getattr(exc, "status_code", None)
            if client_kind == "openai":
                import openai as _openai_for_isinstance

                if not isinstance(exc, _openai_for_isinstance.RateLimitError) and (
                    status is not None and status not in (500, 502, 503, 504)
                ):
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


FORCED_ANSWER_REASON_PREFIX = "answer forced by the app's"


def _should_judge(*, phase: int, status: Status, model_answer: str, no_judge: bool, error: str | None = None) -> bool:
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
    if status == Status.DEGRADED and (error or "").startswith(FORCED_ANSWER_REASON_PREFIX):
        # The app stopped the search and forced this answer (#719). The user received it, so the judge
        # reads it; the degraded status keeps it beside the chosen answers in the matrix, not among them.
        pass
    elif status != Status.DONE:
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


def _status_of_completed(result: dict) -> tuple[Status, str | None]:
    """The unit status of a ``completed`` chat or research result.

    A non-empty answer is not automatically a real engagement with the corpus. ``classify_answer`` is the one
    authority on completed outcomes: it folds the empty answer in beside refusals ("I don't have the
    capability...") and roleplay (a small model writing ``[search_documents: "..."]`` as text). Chat and
    research get the same treatment, so the matrix tells "said something useful" from "punted" the same way.

    An answer the app forced (``stop_reason``: its time budget, context budget or tool-round cap, #719) is one
    the user received but not one the model chose to give: degraded, measured beside the real ones."""
    label, reason = classify_answer(result.get("answer") or "")
    if label != "real":
        return Status.DEGRADED, reason
    if result.get("stop_reason"):
        return Status.DEGRADED, f"{FORCED_ANSWER_REASON_PREFIX} {result['stop_reason']}"
    return Status.DONE, None


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


def _verifier_counts(result: dict) -> dict[str, int]:
    """Count display-only citation verifier verdicts in a Research result.

    The verifier is intentionally off by default. When an operator enables it
    for validation, `HarborClerkClient.run_research()` preserves each
    `verifier_pass` SSE payload under `verifier_verdicts`. These counts make
    metrics.csv useful for deciding whether the signal is release-worthy
    without opening every response artifact.
    """
    counts = {
        "total": 0,
        "supported": 0,
        "partial": 0,
        "unsupported": 0,
        "skipped": 0,
    }
    for item in result.get("verifier_verdicts") or []:
        if not isinstance(item, dict):
            continue
        counts["total"] += 1
        verdict = item.get("verdict")
        if verdict in counts:
            counts[verdict] += 1
    return counts


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
    p.add_argument(
        "--questions-dir",
        type=Path,
        default=None,
        help="read <corpus>.yaml from here instead of questions/ (e.g. questions/keyed). A run is its questions: "
        "use a new --run-id, and do not compare its scores with a run that asked others.",
    )
    p.add_argument("--skip", default="")
    p.add_argument("--phases", default="0-6")
    p.add_argument("--models", default="")
    p.add_argument("--corpora", default="")
    p.add_argument("--depth", default=cfg.DEFAULT_DEPTH)
    p.add_argument("--time-limit-minutes", type=int, default=30)
    p.add_argument("--insecure", action="store_true", help="disable TLS verify (self-signed)")
    p.add_argument("--dry-run", action="store_true", help="run Phase 0 only, no API calls beyond acquire")
    p.add_argument(
        "--no-wait-for-summaries",
        action="store_true",
        help=(
            "start measuring once every search-gating stage is done, while summaries (a background stage) are "
            "still queued. Once a model is active it generates them on its one slot between its own questions, "
            "and later models read them, so the default waits until they are done; metrics.csv records the "
            "backlog at each unit's start either way (summarize_backlog)."
        ),
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        help=(
            "skip the LLM-as-judge call in phases 4 and 5. Use for cheap local-only "
            "runs when you don't want to spend cloud credits on judgments."
        ),
    )
    p.add_argument(
        "--judge-model",
        default=cfg.JUDGE_MODEL,
        help=(
            "the LLM-as-judge model. Scores from different judges are not comparable, so change it on "
            "purpose and for a whole comparison; it is recorded in spend.json. Must be priced in spend.yaml."
        ),
    )
    p.add_argument(
        "--spend-cap-usd",
        type=float,
        default=None,
        help=(
            "lower this run's cloud spend cap below spend.yaml's cap_usd (also HC_EVAL_SPEND_CAP_USD). "
            "It cannot be raised here: that is an edit to spend.yaml, in a PR."
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


QUESTIONS_DIR = Path(__file__).parent.parent / "questions"


def load_questions(questions_dir: Path | None) -> tuple[dict[str, dict], dict[str, dict]]:
    """(questions by corpus, answer keys by corpus). `questions_dir` is another question set in the same format
    (questions/keyed/ has CUAD questions with an answer key); a corpus with no file there keeps the standard
    questions, so --corpora still decides. An answer key is `<corpus>.key.json` beside the questions: human
    labels the harness scores against without a judge or a baseline (runner/answer_key.py). The standard set
    has none, and a corpus without one gets {}."""
    questions, keys = {}, {}
    for c in ("cuad", "enron", "synthetic"):
        override = questions_dir / f"{c}.yaml" if questions_dir else None
        q_path = override if override is not None and override.exists() else QUESTIONS_DIR / f"{c}.yaml"
        if q_path is override:
            log.info("questions for %s from %s", c, q_path)
        questions[c] = yaml.safe_load(q_path.read_text())
        key_path = q_path.with_suffix(".key.json")
        keys[c] = json.loads(key_path.read_text()) if key_path.exists() else {}
        if keys[c]:
            log.info("answer key for %s: %d questions from %s", c, len(keys[c]), key_path)
    return questions, keys


def key_score_cell(key_entry: dict | None, result: dict | None, status: Status | None) -> str:
    """The answer_key_score cell for a unit: the key's score of the answer text, to three places. "" when the
    question has no key, and "" when the unit did not finish (an outage is not a wrong answer: the judge columns
    of that row are blank too, and a reader of the CSV must not count it as 0)."""
    if not key_entry or status not in (Status.DONE, Status.DEGRADED):
        return ""
    return f"{answer_key.score(key_entry, (result or {}).get('answer') or '')['score']:.3f}"


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


KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def missing_keys_for(plan: list[tuple[str, str, int]], env: dict[str, str] | None = None) -> list[str]:
    """The API keys this plan needs and does not have: for every model it will call, its vendor's variable is set
    and not empty. The judge's client is built lazily, so a missing key used to surface at the first phase-4
    verdict, after phase 1 had spent the baseline money, as a warning per unit and a report with no verdicts
    (review of #696). An empty string is what the skill's Keychain command yields when the item is absent."""
    from scripts.test_corpora.runner.judge import vendor_of

    env = os.environ if env is None else env
    missing = []
    for _kind, model, count in plan:
        if count <= 0:
            continue
        var = KEY_ENV[vendor_of(model)]
        if not env.get(var, "").strip() and var not in missing:
            missing.append(var)
    return missing


def _spend_plan(
    units, *, phases: set[int], workdir: Path, judge_model: str, no_judge: bool
) -> list[tuple[str, str, int]]:
    """The cloud calls this invocation will make, as (kind, model, units), for the pre-run estimate: the
    pending units in the phases asked for. state.json holds every phase ever planned for the run id, and
    the run loop skips the ones outside --phases; pricing them all refused the runbook's phase-by-phase
    resume. Every pending phase-4/5 unit counts as one judge call: some will degrade and never be judged,
    and an estimate that refuses a run should err high."""
    pending = [u for u in units if u.status == Status.PENDING and u.phase in phases]
    # Not only phase 0: the run loop acquires a corpus that is missing before any unit that needs it, and
    # the unified pass needs all of them.
    synthetic_docs = 0
    if any(u.corpus in ("synthetic", "unified") for u in pending):
        synthetic_docs = synthetic.planned_generation_count(workdir / "synthetic")
    return [
        ("synthetic_doc", synthetic.GENERATION_MODEL, synthetic_docs),
        ("baseline_question", cfg.BASELINE_MODEL, sum(1 for u in pending if u.phase == 1)),
        ("judge", judge_model, 0 if no_judge else sum(1 for u in pending if u.phase in (4, 5))),
    ]


def _run_info(args: argparse.Namespace) -> dict[str, str]:
    """What the ledger says about the run. A run that judges nothing names no judge, so it neither claims
    one in a report nor trips the no-second-judge guard."""
    if args.mode == "answer-eval":
        model = args.models.split(",")[0].strip() if args.models else "claude-sonnet-4-6"
        return {"run_id": args.run_id, "mode": "answer-eval", "model": model, "judge_model": args.judge_model}
    from scripts.test_corpora.report import suite_commit

    return {
        "run_id": args.run_id,
        "mode": "sweep",
        # The commit that RAN the sweep. A run takes a day; the report may be rendered after a pull.
        "suite_commit": suite_commit(),
        # The machine that ran it, which need not be the one that renders the report.
        "host": platform.node().split(".")[0] or "unknown",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline_model": cfg.BASELINE_MODEL,
        "judge_model": "" if args.no_judge else args.judge_model,
        # A run is its questions. Recorded so that a resume without --questions-dir is refused instead of asking
        # the standard questions under the keyed ids and writing rows with no answer_key_score (review of #697).
        "questions_dir": str(args.questions_dir) if args.questions_dir else "",
    }


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
    scope_folder_id: str | None = None,
) -> dict:
    """Run one local-model question. Assumes the right model is already active and loaded.

    ``scope_folder_id`` (optional): when set, the spawned chat conversation
    or research run is scoped to a single watched folder via PR #415's
    folder-scope feature. Used by ``--no-ingest`` unified-corpus runs to
    isolate per-corpus retrieval on a multi-corpus HC instance. When None,
    HC searches across all visible documents (legacy wipe-and-reload mode).
    """
    scope = {"folder_ids": [scope_folder_id]} if scope_folder_id else None
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
        conv_id, result = hc.run_research(
            question_text, depth=depth, time_limit_minutes=time_limit_minutes, scope=scope
        )
        # Normalize: ResearchDetail returns "report", chat returns "answer".
        # Store under "answer" so all downstream metrics code uses a uniform key.
        normalized_answer = result.get("report") or result.get("answer", "")
        result["answer"] = normalized_answer
    else:
        # Ask flow: create a chat conversation, then send the question, drain SSE
        conv_id = hc.create_conversation(title=f"test-corpora/{corpus}/{model}/{question_id}", scope=scope)
        events = list(hc.stream_ask(conv_id, question_text))
        # Harbor Clerk chat SSE emits {type: "token", content: "<text>"} for tokens.
        # Citations are in the final {type: "done"} event's rag_context.citations field.
        final_text = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        citations: list[dict] = []
        stop_reason: str | None = None
        for e in events:
            if e.get("type") == "done":
                rc = e.get("rag_context") or {}
                for c in rc.get("citations", []) or e.get("citations", []):
                    citations.append(c if isinstance(c, dict) else {"doc_id": c})
                # Why the app stopped the search before the model chose to answer ("time_budget",
                # "context_budget", "tool_rounds"); absent when the model answered on its own (#719).
                stop_reason = e.get("stop_reason") or None
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
        if stop_reason:
            result["stop_reason"] = stop_reason

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

    Three checks: a watch folder whose ``path`` equals ``manifest.ingest_dir``,
    ``document_count() >= 50% of manifest.doc_count``, and no more documents
    than the manifest counts. The floor matches ``_ingest_corpus``'s "ingest
    looks incomplete" check, so anything we'd accept fresh we also accept on
    resume; the ceiling is the same rule as its refusal: an index larger than
    the corpus holds something beside it (#726) and is not this corpus.

    Pure point-in-time check — the caller is responsible for ensuring HC's
    queue is drained before trusting the doc count (see ``_can_skip_ingest``).
    """
    target = str(manifest.ingest_dir)
    folders = hc.watch_folder_list()
    if not any(f.get("path") == target for f in folders):
        return False
    threshold = max(1, int(manifest.doc_count * 0.5))
    actual = hc.document_count()
    if actual > manifest.doc_count:
        log.warning(
            "%s is watched but the index holds %d documents and the corpus has %d: not this corpus, re-ingesting",
            target,
            actual,
            manifest.doc_count,
        )
        return False
    return actual >= threshold


def _settle_summaries(hc: HarborClerkClient, corpus_id: str, wait: bool, stall_seconds: float) -> None:
    """The search-gating stages are done; summaries may not be. Waiting is the default: they do not gate search,
    but a model that runs later reads summaries written earlier (kb_get_document, kb_corpus_overview, ...), and
    unless the host forces Apple Intelligence, an active model writes them itself on its one slot between the
    questions it is being measured on. Every model measures the same corpus only when they are done (#717).

    ``wait=False`` proceeds and says so. A backlog that stops falling is an error (the summarizer is not
    working); one still falling at the deadline (HarborClerkClient.SUMMARY_WAIT_MAX_SECONDS) is proceeded beside
    with a warning (it is working, and
    metrics.csv carries the backlog per unit), never the run's death, which is what #717 was."""
    backlog = hc.summarize_backlog()
    if not backlog:
        return
    if not wait:
        log.warning(
            "%d summaries still queued or running for %s; proceeding without them (--no-wait-for-summaries): "
            "the active model will generate them between its own questions, and later models will read them",
            backlog,
            corpus_id,
        )
        return
    log.info(
        "%d summaries still queued or running for %s; waiting for them so every model measures the same corpus",
        backlog,
        corpus_id,
    )
    outcome = hc.wait_for_summaries(max_stall_seconds=stall_seconds)
    if outcome == "stalled":
        raise RuntimeError(
            f"summaries stalled for {corpus_id}: {hc.summarize_backlog()} still queued or running and the count has "
            f"not fallen for {stall_seconds:.0f}s"
        )
    if outcome == "deadline":
        log.warning(
            "%d summaries still queued or running for %s after the %.0f-hour wait; still falling, so proceeding "
            "beside them: the active model will generate the rest between its own questions, and later models will "
            "read them",
            hc.summarize_backlog(),
            corpus_id,
            hc.SUMMARY_WAIT_MAX_SECONDS / 3600,
        )


def _can_skip_ingest(
    hc: HarborClerkClient,
    manifest: CorpusManifest,
    max_drain_wait: int = 4 * 3600,
    *,
    wait_for_summaries: bool = True,
    summary_stall_seconds: float = 1800,
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
    if not _hc_corpus_matches(hc, manifest):
        return False
    _settle_summaries(hc, manifest.corpus_id, wait_for_summaries, summary_stall_seconds)
    return True


DISPOSABLE_ENV = "HC_EVAL_DISPOSABLE"
# Phases whose units ingest a corpus that is not loaded (1, 4, 5), and the unified pass (6).
INGESTING_PHASES = frozenset({1, 4, 5, 6})
SKIPPED_BEFORE_THE_RUN = "skipped before the run: "
# The corpora the harness ingests, each from <workdir>/<corpus>/ingest.
# metrics.csv, one row per unit. New columns go at the end: a resumed run writes the columns its file already
# has (`metrics_row_for`), so an older file keeps its shape and a reader keys by header, never by position.
METRICS_COLUMNS = (
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
    "verifier_total",
    "verifier_supported",
    "verifier_partial",
    "verifier_unsupported",
    "verifier_skipped",
    "judge_answers_question",
    "answer_key_score",
    "summarize_backlog",
    "stop_reason",
)


def unknown_metrics_columns(columns: list[str]) -> None:
    """Refuse a metrics.csv whose header names a column this code cannot fill. `metrics_row_for` would raise on
    it, but only at the first unit's row, after that unit was marked done: its row would be lost and --resume
    would skip it. A header from a newer branch, or a twin's file copied in, is refused at startup instead."""
    unknown = [c for c in columns if c not in METRICS_COLUMNS]
    if unknown:
        raise SystemExit(
            f"metrics.csv has column(s) this version of the sweep cannot write: {unknown}. It was written by a "
            f"newer sweep or copied from another run; use a fresh --run-id, or run that version."
        )


def metrics_row_for(columns: list[str], **values: object) -> list[object]:
    """The row for `columns`, in their order. A value for a column the file does not have is dropped; a column
    with no value is an error, so a renamed field cannot silently write an empty cell."""
    return [values[column] for column in columns]


HARNESS_CORPORA = ("cuad", "enron", "synthetic", "unified")


class NotDisposable(BaseException):
    """The sweep was about to wipe an instance nobody said it may wipe. Not an Exception, for the reason the
    spend errors are not (runner/spend.py): the run loop's keep-going handlers catch Exception, and this is
    not one bad unit. Both ingest calls sit outside those handlers today; this holds if one is ever moved."""


def _refuse_unless_declared_disposable(api_base: str) -> None:
    """The two conditions that need no network, so they can be checked before the run has written anything."""
    import ipaddress
    from urllib.parse import urlsplit

    if os.environ.get(DISPOSABLE_ENV) != "1":
        raise NotDisposable(
            f"the sweep wipes the instance before each corpus, and {DISPOSABLE_ENV}=1 is not set. Set it only for "
            "an instance whose documents you can lose (the mini's), or pass --no-ingest to measure what is loaded."
        )
    host = urlsplit(api_base).hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise NotDisposable(f"refusing to wipe {api_base}: only an instance on this machine (loopback) is ever wiped")


def _refuse_to_wipe_a_real_instance(hc: HarborClerkClient, api_base: str, workdir: Path) -> None:
    """Ingesting a corpus deletes every watched folder and every document on the instance first
    (`delete-all-documents` truncates documents, uploads and fetched mail alike). That is the design: each
    corpus is measured alone. It is also how a benchmark pointed at the wrong instance destroys a working
    index that took days to build. All of these, every time:

    - the operator declared the instance disposable (HC_EVAL_DISPOSABLE=1);
    - it is on this machine (a loopback API base): nothing remote is ever wiped;
    - every watched folder it has is one of this harness's own ingest directories;
    - it has no connected mailbox: fetched mail is stored, not read in place, and cannot be re-read;
    - if it holds documents, a harness folder is there to account for them. Documents with no harness
      folder are someone's uploads.

    The harness's folders are known by their path. `GET /api/watch/folders` returns no name the harness
    chose: `display_name` is the path's last segment, which for every corpus is "ingest". What this cannot
    see: uploads made to an instance that also holds a harness corpus.
    """
    _refuse_unless_declared_disposable(api_base)
    ours = {(Path(workdir).expanduser() / corpus / "ingest").resolve() for corpus in HARNESS_CORPORA}
    folders = hc.watch_folder_list()
    foreign = [
        str(f.get("path"))
        for f in folders
        if not f.get("path") or Path(str(f["path"])).expanduser().resolve() not in ours
    ]
    if foreign:
        raise NotDisposable(
            f"refusing to wipe {api_base}: it has watched folders that are not this harness's ingest directories "
            f"under {workdir}, which means a real corpus: {foreign}. Remove them yourself if this instance really "
            "is disposable."
        )
    mailboxes = hc.mail_accounts()
    if mailboxes:
        raise NotDisposable(
            f"refusing to wipe {api_base}: it has {len(mailboxes)} connected mailbox(es). Fetched mail is stored, "
            "not read in place; wiping it loses it."
        )
    documents = hc.document_count()
    if documents and not folders:
        raise NotDisposable(
            f"refusing to wipe {api_base}: it holds {documents} document(s) and no folder of this harness's to "
            "account for them, so they are someone's uploads."
        )


def _ingest_corpus(
    hc: HarborClerkClient,
    manifest: CorpusManifest,
    api_base: str,
    workdir: Path,
    *,
    wait_for_summaries: bool = True,
    summary_stall_seconds: float = 1800,
) -> None:
    """Wipe the DB, register the corpus's ingest dir, wait for ingestion to
    fully complete before returning.

    Three-phase wait closes the race where the harness checks ``pipeline_quiet``
    in the brief window between ``watch_folder_add`` returning and the
    watcher actually scanning the folder. Without this, the harness would
    declare the corpus ingested and start research against zero documents.
    """
    _refuse_to_wipe_a_real_instance(hc, api_base, workdir)  # again, here, beside the deletion itself
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
    _settle_summaries(hc, manifest.corpus_id, wait_for_summaries, summary_stall_seconds)

    # Phase 3: the document count. Too few is logged as an error and the run goes on (a file the app refuses is
    # the app's finding). Too many is refused: the folder holds something the manifest does not count, every
    # model would measure it, and once it was the corpus's own answer key (#726, the synthetic fact sidecars).
    actual = hc.document_count()
    if actual > manifest.doc_count:
        raise RuntimeError(
            f"the index holds {actual} documents but {manifest.corpus_id} has {manifest.doc_count}: something "
            f"beside the corpus is in {manifest.ingest_dir}, and every model would measure it"
        )
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


def _skip_what_this_instance_cannot_run(hc: HarborClerkClient, sf: StateFile, phases: set[int]) -> dict[str, str]:
    """Skip the pending units of local models this instance has not downloaded or cannot fit, and say why.

    The model list now comes from the registry, so a sweep on a small machine plans models it will never
    load. Left pending, each of their units fails at activation (the app answers 409 when not even the
    smallest context fits), and the circuit breaker spends its trips on a foregone conclusion. Only PENDING
    units in the phases being run are touched, and every start reconsiders what an earlier one skipped."""
    try:
        listed = {m["id"]: m for m in hc.list_models()}
    except Exception as exc:
        log.warning("could not list the instance's models (%s); not skipping any", exc)
        return {}
    if not listed:
        log.warning("the instance listed no models at all; not skipping any")
        return {}
    # What an earlier start decided is decided again: the model may have been downloaded since, or that
    # start may have been looking at another instance.
    revived = 0
    for u in sf.units():
        if u.status == Status.SKIPPED and u.phase in phases and (u.error or "").startswith(SKIPPED_BEFORE_THE_RUN):
            u.status, u.error = Status.PENDING, None
            revived += 1
    reasons: dict[str, str] = {}
    for u in sf.units():
        if u.status != Status.PENDING or u.phase not in phases or u.phase < 2:
            continue
        info = listed.get(u.model)
        if info is None:
            reason = "this instance's registry does not have it (an older build?)"
        elif not info.get("downloaded"):
            reason = "not downloaded on this instance"
        elif info.get("max_context_here") == 0:
            # Not `fits_here`: that is false whenever the FULL window does not fit, and the app still loads the
            # model with its context clamped (the 35B on a 32 GB Mac runs at about 240K of 262,144 tokens).
            # Activation is refused only when nothing fits, which is what 0 means.
            reason = f"does not fit in this machine's {info.get('system_ram_gb', 0):.0f} GB at any context"
        else:
            continue
        u.status = Status.SKIPPED
        u.error = SKIPPED_BEFORE_THE_RUN + reason
        reasons[u.model] = reason
    for model, reason in sorted(reasons.items()):
        log.warning("skipping %s: %s. Its units are reconsidered at the next start of this run.", model, reason)
    if reasons or revived:
        sf.save()
    return reasons


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

    def _configure_meter() -> spend.SpendMeter:
        # One meter per run, before anything can make a cloud call. A resumed run reads what it already spent.
        m = spend.configure(
            ledger_path=run_dir / spend.LEDGER_NAME,
            cap_usd=args.spend_cap_usd,
            run_info=_run_info(args),
        )
        log.info(
            "cloud spend: cap USD %.2f, already spent %.4f (%s)", m.cap_usd, m.total_usd, run_dir / spend.LEDGER_NAME
        )
        return m

    # --mode retrieval-eval is a separate fast path: no state.json, no per-phase
    # planning, no model switching, no LLM. Dispatch and return before the
    # main sweep loop spins up clients or touches state.
    if args.mode == "retrieval-eval":
        from scripts.test_corpora.runner import retrieval_eval

        return retrieval_eval.main_from_args(args)

    if args.mode == "answer-eval":
        from scripts.test_corpora.runner import answer_eval

        # This mode takes no run lock: two of these on one --run-id would each count from the same ledger.
        try:
            _configure_meter()
            return answer_eval.main_from_args(args)
        except (spend.SpendError, spend.SpendConfigError) as exc:
            log.error("stopped by the spend cap: %s", exc)
            return 3

    # Before anything is registered or written: the two conditions that need no network. A refused start
    # leaves nothing behind, so the same command works once the flag is set.
    may_ingest = not args.no_ingest and not args.dry_run and bool(_phase_range(args.phases) & INGESTING_PHASES)
    if may_ingest:
        try:
            _refuse_unless_declared_disposable(args.api_base)
        except NotDisposable as exc:
            log.error("not starting: %s", exc)
            return 4

    state_path = run_dir / "state.json"
    # What was here before this invocation. A refused start takes back what it made, and only that: the
    # ledger may be older than the state (answer-eval writes one to this directory and no state.json).
    made_here = [p for p in (state_path, run_dir / spend.LEDGER_NAME) if not p.exists()]
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

        # Under the run's lock, so a second process on this --run-id cannot count from the same ledger, and
        # after the guard above, so an invocation that is refused leaves the ledger as it found it.
        meter = _configure_meter()

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

        questions_by_corpus, answer_keys = load_questions(args.questions_dir)

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
        # splitting Phase 4 across machines (32 GB Mac mini for the smaller models, a
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

        # Refuse a run that is estimated over the cap before it has spent anything or touched Harbor Clerk
        # (ADR 0001, decision 6).
        plan = _spend_plan(
            sf.units(), phases=phases, workdir=workdir, judge_model=args.judge_model, no_judge=args.no_judge
        )
        if not args.dry_run:
            estimate = meter.require_within_cap(plan)
            log.info("cloud spend estimate for the pending units: USD %.2f of %.2f", estimate, meter.cap_usd)

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
            # The instance checks, before the first unit and before anything else is decided about this
            # instance: a sweep pointed at the wrong one must not leave its judgements in state.json. (The
            # check beside the deletion itself still stands; refused there, it also ends the run with 4.)
            will_ingest = not args.no_ingest and any(
                u.status == Status.PENDING and u.phase in phases and u.phase in INGESTING_PHASES for u in sf.units()
            )
            if will_ingest:
                try:
                    _refuse_to_wipe_a_real_instance(hc, args.api_base, workdir)
                except (NotDisposable, Exception) as exc:
                    # NotDisposable, or could not look (not logged in, instance down): unverified is not
                    # disposable either.
                    why = exc if isinstance(exc, NotDisposable) else f"could not check that it may be wiped: {exc}"
                    log.error("not starting against %s: %s", args.api_base, why)
                    # Take back what this invocation made, so the same command works once the instance is
                    # right. What was there before stays: a run that existed, a ledger with money in it.
                    for left in made_here:
                        left.unlink(missing_ok=True)
                    return 4
            _skip_what_this_instance_cannot_run(hc, sf, phases)

        # After the instance guards (a wrong instance is the worse mistake, and its exit code is 4) and before
        # any client is built: every model the plan will call has its vendor's key.
        if not args.dry_run and (missing := missing_keys_for(plan)):
            log.error("this run will call a model whose key is not set: %s. Nothing was spent.", ", ".join(missing))
            return 3
        anthro = spend.anthropic_client("baseline_question")
        judge = JudgeClient(model=args.judge_model)

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

        # CSV metrics. The file's own header decides the row shape: a resumed run keeps the columns it started with.
        metrics_path = run_dir / "metrics.csv"
        new_csv = not metrics_path.exists() or metrics_path.stat().st_size == 0
        metrics_columns = list(METRICS_COLUMNS) if new_csv else metrics_path.read_text().splitlines()[0].split(",")
        unknown_metrics_columns(metrics_columns)  # before any unit runs, not after the first is marked done
        for column, why in (
            ("verifier_total", "verifier validation metrics"),
            (
                "judge_answers_question",
                "the judge_answers_question column; the verdicts in it followed completeness and are not comparable with new ones",
            ),
            ("summarize_backlog", "the summarize_backlog column (summaries queued when each unit started)"),
            ("stop_reason", "the stop_reason column (why the app forced an answer, #719)"),
        ):
            if column not in metrics_columns:
                log.warning(
                    "metrics.csv predates %s; preserving its row shape for this resumed run. Use a fresh --run-id.", why
                )
        metrics_f = metrics_path.open("a", newline="")
        metrics_writer = csv.writer(metrics_f)
        if new_csv:
            metrics_writer.writerow(metrics_columns)

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

        # Corpus → folder_id mapping for scoped local runs. Populated lazily
        # on the first _run_local call when --no-ingest is set, so unified
        # multi-corpus HC instances can isolate per-corpus retrieval via
        # PR #415's folder-scope feature. Skipped entirely when ingesting
        # corpora the legacy way (one corpus loaded at a time, no scope
        # needed).
        scope_folder_id_by_corpus: dict[str, str] = {}

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
                if current_corpus_in_db is None and _can_skip_ingest(
                    hc, unified_manifest, wait_for_summaries=not args.no_wait_for_summaries
                ):
                    log.info("HC already has unified corpus loaded — skipping re-ingest")
                    current_corpus_in_db = "unified"
                elif current_corpus_in_db != "unified":
                    _ingest_corpus(
                        hc, unified_manifest, args.api_base, workdir, wait_for_summaries=not args.no_wait_for_summaries
                    )
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
                    if current_corpus_in_db is None and _can_skip_ingest(
                        hc, manifests[u.corpus], wait_for_summaries=not args.no_wait_for_summaries
                    ):
                        log.info("HC already has %s loaded — skipping re-ingest", u.corpus)
                        current_corpus_in_db = u.corpus
                    elif u.corpus != current_corpus_in_db:
                        _ingest_corpus(
                            hc,
                            manifests[u.corpus],
                            args.api_base,
                            workdir,
                            wait_for_summaries=not args.no_wait_for_summaries,
                        )
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
                # Summaries queued when this unit started: 0 is the comparable case. Recorded, not enforced, so a
                # --no-wait-for-summaries run says on every row what it ran beside. Not for phase 0 (no unit is
                # measured) and not on --dry-run (which promises no API calls beyond acquire).
                summarize_backlog_at_start: int | str = ""
                if u.phase > 0 and not args.dry_run:
                    try:
                        summarize_backlog_at_start = hc.summarize_backlog()
                    except Exception:  # a snapshot the API could not give is not a reason to lose the unit
                        summarize_backlog_at_start = ""

                t0 = time.time()
                out: dict = {}
                # Set by the branch that finishes the unit; the exception handlers below fall through to the
                # metrics row with it unset, and the answer key must not read an outage as a wrong answer.
                final_status: Status | None = None
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
                            # The MCP client has its own connection, so it asks hc for the bearer before
                            # every call and has hc log in again when a call is refused: phase 1 outlives
                            # the 30-minute access token whenever ingest or the baselines run long (#681).
                            mcp_session = SyncMcpSession(
                                url=mcp_url,
                                bearer=hc.get_bearer_token,
                                refresh_bearer=hc.refresh_bearer_token,
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
                        # In --no-ingest mode (unified-corpus HC), resolve the
                        # corpus's watch-folder UUID once and reuse it. Passing
                        # the UUID to _run_local makes HC's chat / research
                        # restrict tool calls to that folder (PR #415).
                        scope_folder_id: str | None = None
                        if args.no_ingest and u.corpus != "unified":
                            if u.corpus not in scope_folder_id_by_corpus:
                                scope_folder_id_by_corpus[u.corpus] = hc.folder_id_for_corpus(u.corpus)
                            scope_folder_id = scope_folder_id_by_corpus[u.corpus]

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
                            scope_folder_id=scope_folder_id,
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
                                    scope_folder_id=scope_folder_id,
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
                        if result.get("harness_aborted"):
                            final_status = Status.ERROR
                            error_msg = f"harness aborted research: {result.get('harness_abort_reason', 'unknown')}"
                        elif result_status == "completed":
                            final_status, error_msg = _status_of_completed(result)
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
                # Against the answer key, when the question has one. Needs no baseline: it is what the key is
                # for, and it is computed before the baseline is looked at so an unusable baseline cannot hide it.
                answer_key_score = ""
                if phase in (4, 5):
                    answer_key_score = key_score_cell(
                        answer_keys.get(u.corpus, {}).get(u.question_id), out.get("result"), final_status
                    )
                judge_verdict = ""
                judge_completeness = 0
                judge_answers_question = 0
                spend_stop: spend.SpendError | None = None
                verifier_counts = _verifier_counts(out.get("result", {}) or {})
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
                        # spend cloud credits judging it.
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
                            # cloud credits judging against a baseline we already
                            # know is corrupt.
                            if _should_judge(
                                phase=phase,
                                status=final_status,
                                model_answer=model_answer,
                                no_judge=args.no_judge,
                                error=error_msg,
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
                                except spend.SpendError as exc:
                                    # The run stops here, but not before this unit's row is written: it is
                                    # already DONE in state.json, --resume will skip it, and metrics.csv is
                                    # append-only. Minutes to hours of local compute are in that row.
                                    spend_stop = exc
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
                                    judge_answers_question = v.answers_question

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
                metrics_row = metrics_row_for(
                    metrics_columns,
                    phase=phase,
                    corpus=u.corpus,
                    model=u.model,
                    question_id=u.question_id,
                    depth=u.depth,
                    status=row_unit.status.value if row_unit else "unknown",
                    citation_overlap=f"{co:.3f}",
                    citation_extra=ce,
                    entity_overlap=f"{eo:.3f}",
                    latency_seconds=f"{latency:.1f}",
                    judge_verdict=judge_verdict,
                    judge_completeness=judge_completeness,
                    judge_answers_question=judge_answers_question,
                    verifier_total=verifier_counts["total"],
                    verifier_supported=verifier_counts["supported"],
                    verifier_partial=verifier_counts["partial"],
                    verifier_unsupported=verifier_counts["unsupported"],
                    verifier_skipped=verifier_counts["skipped"],
                    answer_key_score=answer_key_score,
                    summarize_backlog=summarize_backlog_at_start,
                    stop_reason=(out.get("result") or {}).get("stop_reason") or "" if phase in (2, 3, 4, 5, 6) else "",
                )
                metrics_writer.writerow(metrics_row)
                metrics_f.flush()
                if spend_stop is not None:
                    raise spend_stop

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
        log.info(
            "cloud spend: USD %.4f of %.2f over %d calls", meter.total_usd, meter.cap_usd, meter.snapshot()["calls"]
        )
        return 0
    except spend.SpendCapExceeded as exc:
        # Not an Exception, so none of the keep-going handlers above can have swallowed it. Units already
        # done are saved with their rows; the unit that was about to be judged when the cap hit has its row
        # with empty verdict columns. --resume continues the same run against the same ledger.
        log.error("stopped by the spend cap: %s", exc)
        return 3
    except spend.SpendError as exc:
        log.error("stopped by the spend cap: a cloud call that cannot be metered cannot be capped: %s", exc)
        return 3
    except spend.SpendConfigError as exc:
        log.error("stopped by the spend cap: %s", exc)
        return 3
    except NotDisposable as exc:
        # Refused beside the deletion itself, after the run had started: the instance changed under it.
        log.error("stopped before a wipe: %s", exc)
        return 4
    finally:
        sf.release_lock()


if __name__ == "__main__":
    sys.exit(main())
