"""Tests for the sweep supervisor's classifier + state machine.

Network/notification side-effects are not exercised here — we just feed
log lines through ``classify`` and ``supervise`` and assert what comes
out on stdout."""

from __future__ import annotations

import io
import json
from pathlib import Path

from scripts.test_corpora.runner.supervisor import (
    PATTERNS,
    classify,
)

# ── classifier ───────────────────────────────────────────────────────────────


def test_classify_sample_card():
    line = "[Phase 4 · qwen3-8b · cuad · cuad-research-3] elapsed 0:42:13"
    kind, groups = classify(line)
    assert kind == "sample_card"
    assert groups["phase"] == "4"
    assert groups["model"] == "qwen3-8b"
    assert groups["corpus"] == "cuad"
    assert groups["qid"] == "cuad-research-3"


def test_classify_phase_boundary():
    line = "=== Phase 4 complete ==="
    kind, groups = classify(line)
    assert kind == "phase_boundary"
    assert groups["phase"] == "4"


def test_classify_completion():
    line = "2026-05-05 13:00:00 INFO sweep complete after 8347.2s"
    kind, _ = classify(line)
    assert kind == "completion"


def test_classify_error_traceback():
    line = "Traceback (most recent call last):"
    kind, _ = classify(line)
    assert kind == "error"


def test_classify_rate_limit():
    line = "anthropic.RateLimitError: Error code: 429"
    kind, _ = classify(line)
    assert kind == "rate_limit"


def test_classify_does_not_match_hf_unauthenticated_warning():
    """HF prints this on every anonymous download — it's informational, not a
    rate-limit hit. The supervisor must not fire `rate_limit` events on it."""
    line = (
        "WARNING huggingface_hub.utils._http Warning: You are sending "
        "unauthenticated requests to the HF Hub. Please set a HF_TOKEN to "
        "enable higher rate limits and faster downloads."
    )
    assert classify(line) is None


def test_classify_rate_limit_phrases():
    """Exhaustive check of phrases that SHOULD trigger rate_limit."""
    for line in (
        "Error code: 429",
        "anthropic.RateLimitError: too many requests",
        "rate limit exceeded after 5 retries",
        "we got rate-limited by upstream",
        "request was rate limit hit at 17:53",
    ):
        kind, _ = classify(line)
        assert kind == "rate_limit", f"missed: {line!r}"


def test_classify_model_activate():
    line = "2026-05-05 12:34:56 INFO activating model qwen3.6-35b (was qwen3-8b)"
    kind, groups = classify(line)
    assert kind == "model_activate"
    assert groups["model"] == "qwen3.6-35b"


def test_classify_no_match():
    line = "2026-05-05 12:34:56 INFO routine info, nothing special"
    assert classify(line) is None


def test_pattern_priority_completion_beats_phase_boundary():
    """Lines where multiple patterns could match must resolve in declaration order."""
    line = "sweep complete after 100s. Phase 4 complete summary follows..."
    assert PATTERNS["completion"].search(line)
    kind, _ = classify(line)
    assert kind == "completion"


# ── end-to-end ───────────────────────────────────────────────────────────────


def _run_supervisor_against(log_lines: list[str], tmp_path: Path) -> list[dict]:
    log = tmp_path / "test.log"
    # Pre-write completion sentinel so supervisor exits after consuming the input
    log.write_text("\n".join(log_lines) + "\n")

    out = io.StringIO()
    # The tail loop seeks to end-of-file before reading. Pre-populate the file
    # then call supervise — but it'd loop forever waiting for new content. So
    # we patch tail_lines via a local generator.
    from scripts.test_corpora.runner import supervisor as sup

    original = sup.tail_lines

    def fake_tail(path, poll_seconds=1.0):
        yield from log.read_text().splitlines()

    sup.tail_lines = fake_tail
    try:
        sup.supervise(log, out=out, notify=False, stuck_threshold=999)
    finally:
        sup.tail_lines = original

    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_supervisor_emits_phase_boundary_event(tmp_path: Path):
    events = _run_supervisor_against(
        [
            "INFO some unrelated line",
            "=== Phase 4 complete ===",
            "sweep complete after 1234.5s",
        ],
        tmp_path,
    )
    types = [e["event"] for e in events]
    assert "phase_boundary" in types
    assert "completion" in types
    pb = next(e for e in events if e["event"] == "phase_boundary")
    assert pb["phase"] == 4


def test_supervisor_attributes_consecutive_errors_to_active_model(tmp_path: Path):
    """3 consecutive errors against the same model should emit a skip_recommendation."""
    events = _run_supervisor_against(
        [
            "INFO activating model deepseek-r1-8b (was None)",
            "ERROR research task failed: timeout",
            "ERROR research task failed: timeout",
            "ERROR research task failed: timeout",
            "sweep complete after 10s",
        ],
        tmp_path,
    )
    types = [e["event"] for e in events]
    assert "skip_recommendation" in types
    rec = next(e for e in events if e["event"] == "skip_recommendation")
    assert rec["model"] == "deepseek-r1-8b"


def test_supervisor_does_not_flag_stuck_when_log_is_noisy(tmp_path: Path):
    """Phase 1 baseline generation prints lots of unrecognized httpx INFO lines
    but no sample_cards. The supervisor must treat any log activity as
    liveness — only true silence (no lines at all) should trigger 'stuck'."""
    log = tmp_path / "test.log"
    # Many unrecognized lines, then a completion to end the supervisor cleanly.
    log.write_text(
        "\n".join(
            [
                "INFO httpx HTTP Request: POST https://api.anthropic.com/v1/messages 200 OK",
                "INFO httpx HTTP Request: POST https://api.anthropic.com/v1/messages 200 OK",
                "INFO httpx HTTP Request: POST https://api.anthropic.com/v1/messages 200 OK",
                "sweep complete after 100s",
            ]
        )
        + "\n"
    )

    out = io.StringIO()
    from scripts.test_corpora.runner import supervisor as sup

    original = sup.tail_lines

    def fake_tail(path, poll_seconds=1.0):
        yield from log.read_text().splitlines()

    sup.tail_lines = fake_tail
    try:
        # stuck_threshold = 0 means any idle window triggers stuck — but since
        # the lines are read sequentially without an idle gap, none should fire.
        sup.supervise(log, out=out, notify=False, stuck_threshold=0)
    finally:
        sup.tail_lines = original

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    types = [e["event"] for e in events]
    assert "stuck" not in types
    assert "completion" in types


def test_supervisor_resets_error_counter_on_sample_card(tmp_path: Path):
    """A successful completion (sample_card) between errors clears the streak."""
    events = _run_supervisor_against(
        [
            "INFO activating model qwen3-4b (was None)",
            "ERROR something bad",
            "ERROR something else bad",
            "[Phase 4 · qwen3-4b · cuad · cuad-ask-1] elapsed 0:01:00",
            "ERROR another error",
            "sweep complete after 10s",
        ],
        tmp_path,
    )
    types = [e["event"] for e in events]
    # Only 1 error after the sample_card → no skip recommendation
    assert "skip_recommendation" not in types
