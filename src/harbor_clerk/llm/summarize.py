"""Document summarization — adaptive LLM-based with extractive fallback.

Three tiers based on document length:
- Short (<20 chunks): single pass with all content
- Medium (20-100 chunks): strategic sampling (beginning + middle + end)
- Long (100+ chunks): map-reduce (group summaries → final summary)
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import random
import select
import subprocess
import threading
import time
from collections.abc import Callable
from enum import Enum

import httpx

from harbor_clerk.config import get_settings, refresh_llm_settings
from harbor_clerk.llm.models import get_model

logger = logging.getLogger(__name__)

# --- Thresholds ---
_SHORT_THRESHOLD = 20
_LONG_THRESHOLD = 100
_MAX_INPUT_CHARS = 80_000  # hard cap per LLM call
_DEFAULT_CONTEXT_WINDOW = 32_768


class AppleIntelligenceUnavailableError(RuntimeError):
    """Raised when forced Apple Intelligence summaries cannot be produced."""


class AppleIntelligenceDocumentError(RuntimeError):
    """Raised when Apple Intelligence refuses a specific document/request."""


class AppleIntelligenceContentRejectedError(AppleIntelligenceDocumentError):
    """Raised when Apple Intelligence refuses a specific document's content."""


class AppleIntelligenceContextWindowError(AppleIntelligenceDocumentError):
    """Raised when a document is too large for Apple Intelligence after shrinking."""


class _Tier(Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


def _llm_intentionally_deactivated() -> bool:
    """True when the user has deactivated the LLM (no model selected).

    Workers are separate processes from the API and see a stale Settings
    singleton when the user toggles the model from the menubar; we
    `refresh_llm_settings()` first so this answer reflects the current
    on-disk config.json, not whatever the worker booted with.

    On `ConnectError` against llama-server we use this to fail-fast back
    to the AFM / extractive fallback path instead of burning ~95 s of
    jittered retries against a server that's never coming up. The
    "loading" state (model_id set, llama-server still booting) is NOT
    handled here — we can't distinguish it from a crash via settings
    alone, and the existing retry loop is the right thing for genuine
    transients.
    """
    refresh_llm_settings()
    return not get_settings().llm_model_id


# --- System prompts ---
# All prompts request JSON output via response_format for reliable extraction.
# /no_think is kept for Qwen3 (harmless for models that ignore it).
_PROMPT_SHORT = (
    "Summarize this document in 2-3 concise sentences covering the main topic, "
    'key conclusions, and document type. Respond with JSON: {"summary": "..."}. /no_think'
)
_PROMPT_MEDIUM = (
    "You are reading representative excerpts from a longer document. "
    "Summarize the full document in 2-3 concise sentences based on these excerpts. "
    'Respond with JSON: {"summary": "..."}. /no_think'
)
_PROMPT_MAP = (
    "Summarize this section of a longer document in 2-3 sentences. "
    'Focus on the key points and any conclusions. Respond with JSON: {"summary": "..."}. /no_think'
)
_PROMPT_REDUCE = (
    "Below are summaries of different sections of a single document. "
    "Write a unified 2-3 sentence summary of the entire document. "
    'Respond with JSON: {"summary": "..."}. /no_think'
)

# JSON schemas for structured output
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
_DOC_TYPE_SCHEMA = {
    "type": "object",
    "properties": {"document_type": {"type": "string"}},
    "required": ["document_type"],
    "additionalProperties": False,
}


# --- Helpers ---


# Format-category Unicode characters that survive str.strip() (which only
# strips Zs whitespace per str.isspace()) but render as nothing — zero-width
# space, joiner / non-joiner, word joiner, BOM. A wedged or degraded LLM can
# emit these as the entire response body, and without filtering they'd sneak
# past the truthiness checks below and into the DB as a "blank summary
# attributed to the current model" (the symptom that motivated this filter).
_INVISIBLE_CHARS = "​‌‍⁠﻿"
_INVISIBLE_TRANSLATION = str.maketrans("", "", _INVISIBLE_CHARS)


def _has_visible_content(text: str | None) -> bool:
    """Return True iff `text` has at least one character that visibly renders.

    Strips Python-recognised whitespace AND the format-category Unicode chars
    above. Used by `_call_llm` and `generate_summary` so a misbehaving model
    that emits e.g. `"\\u200b"` or `"   "` as its `summary` JSON value is
    treated as a no-result (and the AFM / extractive fallbacks fire) rather
    than persisted as an empty summary attributed to the model.
    """
    if not text:
        return False
    return bool(text.strip().translate(_INVISIBLE_TRANSLATION).strip())


def _extract_marked_answer(text: str) -> str:
    """Extract the final answer from LLM output using ANSWER: or TYPE: markers.

    Thinking models often mix reasoning with the answer. The marker lets us
    reliably extract just the final result from either content or reasoning_content.
    Falls back to the full text if no marker is found (works for non-thinking models).
    """
    if not text:
        return text
    # Check for markers (case-insensitive, may appear after reasoning)
    for marker in ("ANSWER:", "TYPE:"):
        # Find the last occurrence (model may produce multiple during reasoning)
        idx = text.upper().rfind(marker)
        if idx >= 0:
            extracted = text[idx + len(marker) :].strip()
            # Take just the first paragraph after the marker
            first_para = extracted.split("\n\n")[0].strip()
            return first_para if first_para else extracted
    return text


def _compute_max_input_chars(context_window: int | None) -> int:
    """Compute max input chars from model context window, capped at 80K."""
    cw = context_window or _DEFAULT_CONTEXT_WINDOW
    # ~75% of context for input, ~3.5 chars per token
    return min(int(cw * 0.75 * 3.5), _MAX_INPUT_CHARS)


def _select_tier(num_chunks: int) -> _Tier:
    if num_chunks < _SHORT_THRESHOLD:
        return _Tier.SHORT
    elif num_chunks < _LONG_THRESHOLD:
        return _Tier.MEDIUM
    return _Tier.LONG


def _call_llm(
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int = 250,
    timeout: float = 90.0,
    max_attempts: int = 3,
    response_key: str | None = None,
    json_schema: dict | None = None,
    phase: str = "summarize",
) -> str | None:
    """Make an LLM call with retries for transient failures.

    When response_key and json_schema are provided, uses JSON mode via
    response_format and extracts the value at response_key from the JSON
    response. This is far more reliable than parsing markers out of text,
    especially for thinking models that echo prompts or reason verbosely.

    llama-server runs single-slot, so concurrent summarize requests queue up.
    Retries with jittered delays give the queue time to drain.

    The `phase` argument is purely for log attribution — it shows up in
    the per-call timing log line so a `tail -f` of worker-llm.log can
    distinguish short/medium/map/reduce/doc-type calls from each other.
    """
    settings = get_settings()
    url = f"{settings.llama_server_url}/v1/chat/completions"
    payload: dict = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if json_schema:
        # Use json_object mode (simpler, better compatibility with thinking models)
        # rather than json_schema which can confuse gpt-oss and similar.
        payload["response_format"] = {"type": "json_object"}

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        call_started = time.perf_counter()
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            elapsed = time.perf_counter() - call_started
            if resp.status_code in {503, 429} and attempt < max_attempts:
                delay = 5 + random.uniform(0, 10 * attempt)
                logger.info(
                    "LLM call phase=%s elapsed=%.1fs http_status=%d (attempt %d/%d, retrying in %.0fs)",
                    phase,
                    elapsed,
                    resp.status_code,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            cap_hit = (
                isinstance(completion_tokens, int) and isinstance(max_tokens, int) and completion_tokens >= max_tokens
            )
            logger.info(
                "LLM call phase=%s elapsed=%.1fs prompt_tokens=%s completion_tokens=%s max_tokens=%s%s",
                phase,
                elapsed,
                prompt_tokens,
                completion_tokens,
                max_tokens,
                " CAP_HIT" if cap_hit else "",
            )
            # Thinking models (DeepSeek R1, gpt-oss) may return empty content
            # with the answer embedded in reasoning_content
            if not content and msg.get("reasoning_content"):
                content = msg["reasoning_content"].strip()

            # JSON mode: parse and extract the requested key
            if response_key and content:
                import json as _json
                import re as _re

                # Strip markdown code fences if present (```json ... ```)
                json_text = content
                fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", json_text, _re.DOTALL)
                if fence:
                    json_text = fence.group(1)
                # Find the first { ... } block
                brace_start = json_text.find("{")
                brace_end = json_text.rfind("}")
                if brace_start >= 0 and brace_end > brace_start:
                    json_text = json_text[brace_start : brace_end + 1]
                try:
                    parsed = _json.loads(json_text)
                    extracted = parsed.get(response_key, "")
                    # Strip first, THEN check truthiness — a value of
                    # "   " or " " is truthy as an unstripped string
                    # but blank after strip; without this, _call_llm would
                    # return "" which the summarize callers treat as falsy
                    # but `_has_visible_content` covers exotic format-only
                    # characters too.
                    candidate = str(extracted).strip() if extracted else ""
                    return candidate if _has_visible_content(candidate) else None
                except (ValueError, AttributeError):
                    logger.warning("LLM call phase=%s JSON parse failed: %s", phase, content[:200])
                    return None

            # Non-JSON mode: extract marked answer
            content = _extract_marked_answer(content)
            return content if _has_visible_content(content) else None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            elapsed = time.perf_counter() - call_started
            last_err = e
            # Fail-fast when the user has intentionally deactivated the
            # LLM — retrying for ~95 s only to fall through to the same
            # AFM / extractive fallback path makes the worker spend a
            # full minute of wall time per stalled summarize, which the
            # user feels. On loading / genuine-transient cases we still
            # retry with backoff.
            if isinstance(e, httpx.ConnectError) and _llm_intentionally_deactivated():
                logger.info(
                    "LLM call phase=%s elapsed=%.1fs ConnectError + LLM deactivated — skipping %d remaining retries",
                    phase,
                    elapsed,
                    max_attempts - attempt,
                )
                return None
            if attempt < max_attempts:
                delay = 5 + random.uniform(0, 10 * attempt)
                logger.info(
                    "LLM call phase=%s elapsed=%.1fs failed (%s), attempt %d/%d, retrying in %.0fs",
                    phase,
                    elapsed,
                    type(e).__name__,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
        except Exception:
            elapsed = time.perf_counter() - call_started
            logger.warning("LLM call phase=%s elapsed=%.1fs failed (non-retryable)", phase, elapsed, exc_info=True)
            return None

    logger.warning("LLM call phase=%s failed after %d attempts: %s", phase, max_attempts, last_err)
    return None


def _sample_chunks(chunks: list[str], max_chars: int) -> str:
    """Select representative chunks: first 3 + last 2 + evenly-spaced middle, within char budget."""
    if not chunks:
        return ""

    n = len(chunks)
    if n <= 5:
        text = "\n\n".join(chunks)
        return text[:max_chars]

    # Always include first 3 and last 2
    head = list(range(min(3, n)))
    tail = list(range(max(n - 2, 0), n))

    # Evenly-spaced middle indices (excluding head/tail)
    middle_start = len(head)
    middle_end = n - len(tail)
    if middle_end > middle_start:
        # Pick up to 10 evenly-spaced middle chunks
        num_middle = min(10, middle_end - middle_start)
        step = (middle_end - middle_start) / (num_middle + 1)
        middle = [int(middle_start + step * (i + 1)) for i in range(num_middle)]
    else:
        middle = []

    indices = sorted(set(head + middle + tail))

    # Build text within char budget
    parts: list[str] = []
    total = 0
    for idx in indices:
        chunk = chunks[idx]
        addition = len(chunk) + (2 if parts else 0)  # +2 for \n\n separator
        if total + addition > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += addition

    return "\n\n".join(parts)


def _group_chunks_for_mapreduce(chunks: list[str], chars_per_group: int) -> list[str]:
    """Group sequential chunks into groups respecting char limits."""
    groups: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for chunk in chunks:
        addition = len(chunk) + (2 if current_parts else 0)
        if current_parts and current_len + addition > chars_per_group:
            groups.append("\n\n".join(current_parts))
            current_parts = [chunk]
            current_len = len(chunk)
        else:
            current_parts.append(chunk)
            current_len += addition

    if current_parts:
        groups.append("\n\n".join(current_parts))

    return groups


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Find the last sentence-ending punctuation
    for i in range(len(truncated) - 1, -1, -1):
        if truncated[i] in ".!?" and (i + 1 >= len(truncated) or truncated[i + 1] in " \n\t"):
            return truncated[: i + 1]
    # No sentence boundary found — fall back to last space to avoid mid-word
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space] + "\u2026"
    return truncated


def _find_apple_summarize_binary() -> str | None:
    """Locate the apple-summarize CLI binary. Returns None if unavailable."""
    import os

    binary = os.environ.get("APPLE_SUMMARIZE_PATH", "")
    if binary:
        return binary

    # Find app bundle Resources dir from the Python executable path
    # e.g. .../HarborClerkServer.app/Contents/Resources/venv/bin/python3
    import sys
    from pathlib import Path

    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.name == "Resources":
            candidate = parent / "apple-summarize"
            if candidate.exists():
                return str(candidate)
            break
    return None


# Module-level state for the long-running apple-summarize daemon. The daemon
# keeps process startup out of the per-document path; the Swift helper still
# creates a fresh LanguageModelSession per request so transcript state cannot
# leak across unrelated documents in a corpus-sized batch.
# `_daemon_lock` serializes both daemon-startup AND request/response on
# stdin/stdout (the daemon processes requests strictly sequentially; a
# parallel call would interleave the JSON lines).
_daemon_lock = threading.Lock()
_daemon: subprocess.Popen | None = None
# Per-request read timeout. Historical successful AFM summaries were usually
# ~8-10 s; when AFM hangs, falling back quickly matters more than preserving a
# very long wait for a single best-effort stage.
_DAEMON_READ_TIMEOUT_S = 30.0
_AFM_CIRCUIT_BREAKER_SECONDS = 1.0
_AFM_LAST_FAILURE_REASON_SECONDS = 60.0
_afm_disabled_until_monotonic = 0.0
_afm_disabled_reason = ""
_afm_last_failure_until_monotonic = 0.0
_afm_last_failure_reason = ""


def _apple_intelligence_disabled_reason() -> str | None:
    if _afm_disabled_until_monotonic <= time.monotonic():
        return None
    return _afm_disabled_reason or "temporarily disabled"


def apple_intelligence_cooldown() -> tuple[float, str] | None:
    """Return remaining AFM cooldown seconds and reason, if active."""
    remaining = _afm_disabled_until_monotonic - time.monotonic()
    if remaining <= 0:
        return None
    return remaining, _afm_disabled_reason or "temporarily disabled"


def _recent_apple_intelligence_failure_reason() -> str | None:
    if _afm_last_failure_until_monotonic <= time.monotonic():
        return None
    return _afm_last_failure_reason or None


def _apple_intelligence_unavailable_reason() -> str:
    disabled_reason = _apple_intelligence_disabled_reason()
    if disabled_reason:
        return disabled_reason
    recent_reason = _recent_apple_intelligence_failure_reason()
    if recent_reason:
        return recent_reason
    if _find_apple_summarize_binary() is None:
        return "apple-summarize helper not found"
    return "no summary returned"


def _remember_apple_intelligence_failure(reason: str) -> None:
    global _afm_last_failure_until_monotonic, _afm_last_failure_reason
    _afm_last_failure_until_monotonic = time.monotonic() + _AFM_LAST_FAILURE_REASON_SECONDS
    _afm_last_failure_reason = reason


def _clear_apple_intelligence_failure() -> None:
    global _afm_last_failure_until_monotonic, _afm_last_failure_reason
    _afm_last_failure_until_monotonic = 0.0
    _afm_last_failure_reason = ""


def _is_apple_intelligence_content_rejection(reason: str) -> bool:
    normalized = reason.lower()
    return "unsafe" in normalized or "safety" in normalized


def _is_apple_intelligence_context_window_error(reason: str) -> bool:
    normalized = reason.lower()
    return "context window" in normalized or "context size" in normalized


def _disable_apple_intelligence(reason: str) -> None:
    global _afm_disabled_until_monotonic, _afm_disabled_reason
    _afm_disabled_until_monotonic = time.monotonic() + _AFM_CIRCUIT_BREAKER_SECONDS
    _afm_disabled_reason = reason
    _remember_apple_intelligence_failure(reason)


def _stop_daemon() -> None:
    """Tear down the cached apple-summarize daemon. Safe to call when no
    daemon is running. Caller MUST hold `_daemon_lock` or be in atexit."""
    global _daemon
    if _daemon is None:
        return
    try:
        if _daemon.stdin is not None:
            try:
                _daemon.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            _daemon.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _daemon.kill()
            with contextlib.suppress(Exception):
                _daemon.wait(timeout=1)
    except Exception:
        logger.debug("Error stopping apple-summarize daemon", exc_info=True)
    finally:
        _daemon = None


def _get_or_start_daemon() -> subprocess.Popen | None:
    """Return the cached daemon process, starting one if necessary. Returns
    None when the apple-summarize binary isn't available (Docker / older
    macOS / unbundled dev runs). Caller MUST hold `_daemon_lock`."""
    global _daemon
    if _daemon is not None and _daemon.poll() is None:
        return _daemon
    if _daemon is not None:
        # Previous daemon died; clear before respawn so the atexit
        # handler doesn't try to clean up a zombie.
        _daemon = None
    binary = _find_apple_summarize_binary()
    if not binary:
        return None
    try:
        _daemon = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # DEVNULL, NOT PIPE — Python never reads from daemon stderr, and an
            # unread stderr pipe with the OS-default 64KB buffer can deadlock
            # the daemon if it produces enough output (e.g. Foundation Models
            # framework diagnostics during a startup failure on an unsupported
            # macOS) while Python is blocked in select() on stdout. DEVNULL
            # discards on the OS side so there's no buffer to fill.
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered on the Python end (daemon already line-buffers via FileHandle.write)
        )
        # Register cleanup once per process. Repeated calls to
        # atexit.register would stack handlers; the global sentinel avoids that.
        global _atexit_registered
        if not _atexit_registered:
            atexit.register(_atexit_stop_daemon)
            _atexit_registered = True
        return _daemon
    except (FileNotFoundError, OSError) as exc:
        logger.debug("Failed to start apple-summarize daemon: %s", exc)
        return None


_atexit_registered = False


def _atexit_stop_daemon() -> None:
    """atexit shim that takes the lock before tearing down."""
    with _daemon_lock:
        _stop_daemon()


def _apple_intelligence_call(text: str, mode: str, max_chars: int = 500) -> str | None:
    """Send a single request to the cached apple-summarize daemon. Returns
    None on any failure (binary not present, daemon died mid-request, parse
    error, AFM error response, read timeout). Transport failures trip a
    process-local cooldown so a wedged AFM daemon cannot stall every document
    in a corpus-sized summarize backlog."""
    # Cap at 12,000 chars for ~4K token context
    text = text[:12_000]
    request_line = json.dumps({"prompt": text, "max_chars": max_chars, "mode": mode}) + "\n"

    with _daemon_lock:
        disabled_reason = _apple_intelligence_disabled_reason()
        if disabled_reason:
            logger.info("Apple Intelligence skipped: %s", disabled_reason)
            return None

        daemon = _get_or_start_daemon()
        if daemon is None:
            logger.debug("apple-summarize binary not found — skipping Apple Intelligence")
            return None
        try:
            assert daemon.stdin is not None and daemon.stdout is not None  # bound at Popen-time
            daemon.stdin.write(request_line)
            daemon.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            logger.warning("apple-summarize daemon stdin broken (%s); disabling temporarily", exc)
            _disable_apple_intelligence("daemon stdin broken")
            _stop_daemon()
            return None

        # Wait up to _DAEMON_READ_TIMEOUT_S for a response line. Using
        # select() instead of a blocking readline() keeps a hung daemon
        # from wedging the worker forever — the existing one-shot path
        # capped at 120 s and we preserve that contract.
        ready, _, _ = select.select([daemon.stdout], [], [], _DAEMON_READ_TIMEOUT_S)
        if not ready:
            logger.warning(
                "apple-summarize daemon timed out after %.0fs; disabling temporarily", _DAEMON_READ_TIMEOUT_S
            )
            _disable_apple_intelligence("daemon timed out")
            _stop_daemon()
            return None
        try:
            line = daemon.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            logger.warning("apple-summarize daemon stdout broken (%s); disabling temporarily", exc)
            _disable_apple_intelligence("daemon stdout broken")
            _stop_daemon()
            return None

    # JSON parsing happens outside the lock — the wire I/O is complete and
    # subsequent callers can proceed in parallel against the daemon.
    if not line:
        logger.debug("apple-summarize daemon closed stdout; disabling temporarily")
        with _daemon_lock:
            _disable_apple_intelligence("daemon closed stdout")
            _stop_daemon()
        return None
    try:
        response = json.loads(line)
    except ValueError:
        logger.debug("apple-summarize returned non-JSON line: %.200s", line)
        _remember_apple_intelligence_failure("helper returned invalid JSON")
        return None
    if "error" in response:
        error = str(response.get("error", ""))[:200] or "helper returned an error"
        logger.debug("apple-summarize error response: %.200s", error)
        _remember_apple_intelligence_failure(error)
        return None
    result = (response.get("result") or "").strip()
    if not result:
        _remember_apple_intelligence_failure("helper returned an empty summary")
        return None
    _clear_apple_intelligence_failure()
    logger.info("Apple Intelligence %s: %d chars", mode, len(result))
    return result


def _apple_intelligence_summary(chunks: list[str], max_chars: int) -> str | None:
    """Get a document summary via Apple Intelligence. macOS native only."""
    # Build input text: same sampling as LLM path
    if len(chunks) < 20:
        text = "\n\n".join(chunks)
    else:
        # Strategic sampling: first 3 + last 2 + evenly-spaced middle
        sampled = chunks[:3]
        middle_count = min(5, len(chunks) - 5)
        if middle_count > 0:
            step = max(1, (len(chunks) - 5) // middle_count)
            sampled += chunks[3::step][:middle_count]
        sampled += chunks[-2:]
        text = "\n\n".join(sampled)

    for limit in (12_000, 8_000, 5_000, 3_000):
        result = _apple_intelligence_call(text[:limit], mode="summary", max_chars=max_chars)
        if result:
            return result
        reason = _recent_apple_intelligence_failure_reason() or ""
        if not _is_apple_intelligence_context_window_error(reason):
            return None
        logger.info("Apple Intelligence summary exceeded context at %d chars; retrying with shorter input", limit)
    return None


def _apple_intelligence_doc_type(chunks: list[str]) -> str | None:
    """Classify document type via Apple Intelligence. macOS native only."""
    sample = "\n\n".join(chunks[:5])[:2000]
    result = _apple_intelligence_call(sample, mode="doc_type")
    if result:
        # Clean up: remove quotes, periods, newlines; cap length
        doc_type = result.strip().strip("\"'.").split("\n")[0].strip()
        if len(doc_type) > 50:
            doc_type = doc_type[:50]
        return doc_type if doc_type else None
    return None


def _extractive_fallback(chunks: list[str], max_chars: int) -> str:
    """Take first substantial paragraph from initial chunks as summary."""
    text = "\n\n".join(chunks[:5])
    paragraphs = text.split("\n\n")
    for p in paragraphs:
        p = p.strip()
        if len(p) >= 80:
            return p[:max_chars]
    return text[:max_chars].strip()


# --- Tier implementations ---


def _summarize_short(chunks: list[str], max_input_chars: int) -> str | None:
    """Short docs: concat all chunks, single LLM call."""
    text = "\n\n".join(chunks)[:max_input_chars]
    return _call_llm(
        _PROMPT_SHORT,
        text,
        max_tokens=500,
        timeout=120.0,
        response_key="summary",
        json_schema=_SUMMARY_SCHEMA,
        phase="summarize-short",
    )


def _summarize_medium(chunks: list[str], max_input_chars: int) -> str | None:
    """Medium docs: strategic sampling, single LLM call."""
    text = _sample_chunks(chunks, max_input_chars)
    return _call_llm(
        _PROMPT_MEDIUM,
        text,
        max_tokens=500,
        timeout=120.0,
        response_key="summary",
        json_schema=_SUMMARY_SCHEMA,
        phase="summarize-medium",
    )


def _summarize_long(
    chunks: list[str],
    max_input_chars: int,
    *,
    yield_check: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str | None:
    """Long docs: map-reduce — group summaries then final summary.

    `yield_check`, if provided, is called before each LLM sub-call so the
    summarize worker can yield to interactive chat/research workloads
    between map iterations and before the reduce step.

    `progress_callback`, if provided, is called with (chunks_done,
    total_chunks) before each LLM sub-call so the worker can update its
    IngestionJob row's progress counters. total_chunks counts each map
    call plus the reduce as one chunk each.
    """
    groups = _group_chunks_for_mapreduce(chunks, max_input_chars)
    logger.info("Map-reduce summarization: %d groups from %d chunks", len(groups), len(chunks))

    total_chunks = len(groups) + 1  # +1 for the reduce step
    # Map step: summarize each group (fewer retries to stay within stage timeout)
    section_summaries: list[str] = []
    for idx, group in enumerate(groups):
        if yield_check is not None:
            yield_check()
        if progress_callback is not None:
            progress_callback(idx, total_chunks)
        result = _call_llm(
            _PROMPT_MAP,
            group,
            max_tokens=300,
            timeout=90.0,
            max_attempts=2,
            response_key="summary",
            json_schema=_SUMMARY_SCHEMA,
            phase=f"summarize-map[{idx + 1}/{len(groups)}]",
        )
        if result:
            section_summaries.append(result[:300])
        else:
            # Extractive snippet for failed group
            snippet = group[:200].strip()
            if snippet:
                section_summaries.append(snippet)

    if not section_summaries:
        return None

    # Reduce step: combine section summaries into final
    if yield_check is not None:
        yield_check()
    if progress_callback is not None:
        progress_callback(len(groups), total_chunks)
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(section_summaries))
    reduce_input = numbered[:max_input_chars]
    return _call_llm(
        _PROMPT_REDUCE,
        reduce_input,
        max_tokens=500,
        timeout=120.0,
        response_key="summary",
        json_schema=_SUMMARY_SCHEMA,
        phase="summarize-reduce",
    )


# --- Main entry point ---


_MIME_TYPE_MAP = {
    "application/pdf": "PDF Document",
    "text/plain": "Text File",
    "text/markdown": "Text File",
    "text/csv": "Spreadsheet",
    "image/jpeg": "Image",
    "image/png": "Image",
    "image/tiff": "Image",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word Document",
    "application/msword": "Word Document",
    "application/vnd.oasis.opendocument.text": "Word Document",
    "application/rtf": "Word Document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Presentation",
    "application/epub+zip": "E-Book",
    "text/html": "Web Page",
    "message/rfc822": "Email",
}


def _mime_to_doc_type(mime_type: str) -> str:
    if mime_type in _MIME_TYPE_MAP:
        return _MIME_TYPE_MAP[mime_type]
    if mime_type.startswith("image/"):
        return "Image"
    if mime_type.startswith("text/"):
        return "Text File"
    return "Document"


def classify_doc_type(chunks: list[str], mime_type: str = "") -> str:
    """Classify document type. Fallback chain: local LLM → Apple Intelligence → MIME type."""
    settings = get_settings()

    if settings.summary_force_apple_intelligence:
        ai_type = _apple_intelligence_doc_type(chunks)
        if ai_type:
            return ai_type
        logger.info("Apple Intelligence unavailable while forced for doc_type — using MIME fallback")
        return _mime_to_doc_type(mime_type)

    # Try local LLM first (if a model is active)
    if settings.llm_model_id:
        sample = "\n\n".join(chunks)[:2000]
        prompt = (
            "Classify this document with a short phrase (2-4 words) like: "
            "Legal Contract, Tax Return, Meeting Notes, Research Paper, Invoice, "
            "Recipe, Resume, Technical Manual, News Article, Personal Letter, "
            "Philosophical Essay, Novel, Religious Text, Diary, etc. "
            'Respond with JSON: {"document_type": "..."}. /no_think'
        )
        result = _call_llm(
            prompt,
            sample,
            max_tokens=600,  # Thinking models need room to reason before emitting JSON
            max_attempts=1,
            response_key="document_type",
            json_schema=_DOC_TYPE_SCHEMA,
            phase="doc-type",
        )
        if result:
            doc_type = result.strip().strip("\"'.")
            if len(doc_type) > 50:
                doc_type = doc_type[:50]
            return doc_type
        logger.warning("LLM doc_type returned no result — trying Apple Intelligence")
    else:
        logger.info("No language model active — trying Apple Intelligence for doc_type")

    # Try Apple Intelligence (macOS native only)
    ai_type = _apple_intelligence_doc_type(chunks)
    if ai_type:
        return ai_type

    return _mime_to_doc_type(mime_type)


def generate_summary(
    chunks: list[str],
    max_chars: int | None = None,
    *,
    yield_check: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[str, str]:
    """Generate a summary for a document from its chunks.

    Uses an adaptive strategy based on document length:
    - Short (<20 chunks): single pass with all content
    - Medium (20-100 chunks): strategic sampling + single LLM call
    - Long (100+ chunks): map-reduce (group summaries → final)

    Falls back to extractive heuristic when no LLM is available. Raises
    AppleIntelligenceUnavailableError only when the operator explicitly
    forced Apple Intelligence summaries and AFM cannot produce one.

    `yield_check`, if provided, is called between map-reduce sub-calls in
    the long tier so the caller can yield to interactive workloads.

    `progress_callback`, if provided, is called with (chunks_done,
    total_chunks) between map-reduce sub-calls so the caller can surface
    mid-flight progress (e.g. write to the IngestionJob row's progress
    counters). Only the long tier reports progress — short and medium
    are single-call.

    Returns (summary_text, model_used) where model_used is the LLM model id
    or "extractive" for the heuristic fallback.
    """
    settings = get_settings()
    if max_chars is None:
        max_chars = settings.summary_max_chars

    # Filter out empty/whitespace-only chunks
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return "", ""

    # User has opted to skip the local LLM for summaries entirely
    # (Models page → "Always use Apple Intelligence for summaries"). Skip
    # straight to AFM. If AFM is unavailable for any reason (binary not
    # found, macOS version too old, timeout), fail closed instead of silently
    # generating a corpus-sized batch of extractive summaries.
    if settings.summary_force_apple_intelligence:
        logger.info("Summarize forced to Apple Intelligence by setting (skipping local LLM)")
        ai_summary = _apple_intelligence_summary(chunks, max_chars)
        if _has_visible_content(ai_summary):
            return ai_summary, "apple-intelligence"  # type: ignore[return-value]
        reason = _apple_intelligence_unavailable_reason()
        if _is_apple_intelligence_content_rejection(reason):
            logger.error(
                "Apple Intelligence refused this document while forced; refusing extractive fallback (%s)", reason
            )
            raise AppleIntelligenceContentRejectedError(
                f"Apple Intelligence refused to summarize this document: {reason}"
            )
        if _is_apple_intelligence_context_window_error(reason):
            logger.error(
                "Apple Intelligence exceeded context for this document while forced; refusing extractive fallback (%s)",
                reason,
            )
            raise AppleIntelligenceContextWindowError(
                f"Apple Intelligence could not summarize this document within its context window: {reason}"
            )
        if _apple_intelligence_disabled_reason() is None:
            _disable_apple_intelligence(reason)
        logger.error("Apple Intelligence unavailable while forced; refusing extractive fallback (%s)", reason)
        raise AppleIntelligenceUnavailableError(f"Apple Intelligence summaries are enabled but unavailable: {reason}")

    # Try LLM if a model is active
    if settings.llm_model_id:
        model = get_model(settings.llm_model_id)
        context_window = model.context_window if model else None
        max_input_chars = _compute_max_input_chars(context_window)

        tier = _select_tier(len(chunks))
        logger.info(
            "Summarizing %d chunks via %s tier (model=%s, max_input=%d)",
            len(chunks),
            tier.value,
            settings.llm_model_id,
            max_input_chars,
        )

        stage_started = time.perf_counter()
        if tier == _Tier.SHORT:
            result = _summarize_short(chunks, max_input_chars)
        elif tier == _Tier.MEDIUM:
            result = _summarize_medium(chunks, max_input_chars)
        else:
            result = _summarize_long(
                chunks,
                max_input_chars,
                yield_check=yield_check,
                progress_callback=progress_callback,
            )
        stage_elapsed = time.perf_counter() - stage_started

        # One-line per-doc summary so a `tail -f` of worker-llm.log yields
        # an at-a-glance view of how long each summarize stage really took
        # (the per-call lines above add up to the totals here, but having
        # both makes diagnosis quick — grep for "Summarize complete" to
        # see total wall time per doc, drill into per-call lines for the
        # breakdown).
        logger.info(
            "Summarize complete: tier=%s elapsed=%.1fs result=%s model=%s chunks=%d",
            tier.value,
            stage_elapsed,
            "ok" if result else "no-result",
            settings.llm_model_id,
            len(chunks),
        )

        if _has_visible_content(result):
            return _truncate_at_sentence(result, max_chars), settings.llm_model_id
        logger.warning("LLM summarization returned no visible content — trying Apple Intelligence")
    else:
        logger.info("No language model active — trying Apple Intelligence fallback")

    # Try Apple Intelligence (macOS native only). Use `_has_visible_content`
    # instead of bare truthiness to match the LLM-path check above and the
    # forced-AFM branch — invisible-only Unicode (zero-width chars, format
    # codes) is a real model failure mode and should fall through to
    # extractive rather than getting stored as a summary.
    ai_summary = _apple_intelligence_summary(chunks, max_chars)
    if _has_visible_content(ai_summary):
        return ai_summary, "apple-intelligence"  # type: ignore[return-value]

    if not settings.llm_model_id:
        logger.warning(
            "No language model active and Apple Intelligence unavailable — "
            "document summary will be lower quality. Activate a model in System Settings > Models."
        )

    return _extractive_fallback(chunks, max_chars), "extractive"
