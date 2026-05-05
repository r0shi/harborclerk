"""Watchdog for an in-flight sweep.

Tails the harness log, recognises three classes of events, posts macOS
notifications, and applies simple auto-skip rules so the user can leave
the sweep unattended for many hours.

The supervisor is intentionally dumb — it doesn't make subjective calls
about partial answers or near-OOM conditions. For ambiguous events it
posts a notification and pauses (no automatic recovery). A Claude
subagent, with broader judgment, can be layered on top by watching the
same JSON event stream the supervisor prints to stdout.

Usage:
    python -m scripts.test_corpora.runner.supervisor \\
        --log-file phase4-mini.log \\
        [--rules rules.yaml] \\
        [--no-notify]

Output: one JSON event per line on stdout. Each event has at least
``{"event": <type>, "line": <log line>}`` plus type-specific fields.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

# ── Patterns ──────────────────────────────────────────────────────────────────
#
# Each pattern matches a class of log line emitted by the harness.
# The harness prints:
#   - sample cards every 5 completions: "[Phase 4 · qwen3-8b · cuad · q07] elapsed 0:42:13"
#   - phase boundary tables: "=== Phase 4 complete ==="
#   - completion: "sweep complete after Ns"
#   - errors: "ERROR" / "Traceback" / etc.
#
# Patterns are evaluated in declaration order; first match wins.

PATTERNS: dict[str, re.Pattern] = {
    "completion": re.compile(r"sweep complete after"),
    "phase_boundary": re.compile(r"=== Phase (?P<phase>\d+) complete ==="),
    "sample_card": re.compile(r"^\[Phase (?P<phase>\d+) · (?P<model>[\w.\-]+) · (?P<corpus>\w+) · (?P<qid>[\w-]+)\]"),
    "blocked": re.compile(r"sweep stops|^blocked\b", re.IGNORECASE),
    "error": re.compile(r"\b(ERROR|Traceback|OOM|Killed|Connection refused|HTTPStatusError|ReadTimeout)\b"),
    # Match real rate-limit signals only:
    #   - HTTP 429 status codes
    #   - SDK exception class names (Anthropic, OpenAI)
    #   - explicit "exceeded" / "hit" / "throttled" wording
    # Crucially this does NOT match HuggingFace's informational warning
    # "Please set a HF_TOKEN to enable higher rate limits", which is benign.
    "rate_limit": re.compile(
        r"\b429\b|RateLimitError|rate.?limit(?:[\- ](?:exceed|hit|reach|throttl)|ed\b)",
        re.IGNORECASE,
    ),
    "model_activate": re.compile(r"activating model (?P<model>[\w.\-]+)"),
    "ingest_start": re.compile(r"clearing existing watch folders|delete_all_documents before ingesting"),
}

# How long without ANY recognised activity before we flag the sweep as stuck.
STUCK_THRESHOLD_SECONDS = 30 * 60


# ── Auto-skip rules ──────────────────────────────────────────────────────────
#
# Per-(model, corpus) error counter. When a (model, corpus) accumulates this many
# consecutive ERROR events without an intervening sample_card, the supervisor
# emits a "skip_recommendation" event. A wrapping Claude subagent (or the user)
# then runs `sweep --skip "model=<m>"` to mark all that model's units as SKIPPED.

CONSECUTIVE_ERROR_THRESHOLD = 3


@dataclasses.dataclass
class SupervisorState:
    last_progress_at: float
    # Per-model error streak. A sample_card for that model clears it. The streak
    # is intentionally model-only, not per-(model, corpus): if a model is broken
    # for one corpus it'll usually be broken for all of them, so attributing the
    # streak to the model speeds up the skip recommendation.
    consecutive_errors: dict[str, int]
    last_phase_seen: int | None
    current_model: str | None

    @classmethod
    def fresh(cls) -> SupervisorState:
        return cls(
            last_progress_at=time.time(),
            consecutive_errors=defaultdict(int),
            last_phase_seen=None,
            current_model=None,
        )


# ── Notifications ─────────────────────────────────────────────────────────────


def macos_notify(title: str, message: str) -> None:
    """Post a system notification via osascript. Silent failure on non-macOS."""
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{message[:200]}" with title "{title[:80]}"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# ── Tail loop ─────────────────────────────────────────────────────────────────


def tail_lines(path: Path, poll_seconds: float = 1.0) -> Iterator[str | None]:
    """Yield log lines as they are appended. Yields ``None`` whenever the
    poll interval elapses without new content — callers use that for
    'stuck' detection."""
    with path.open("r") as f:
        f.seek(0, 2)  # tail mode
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(poll_seconds)
                yield None


def classify(line: str) -> tuple[str, dict] | None:
    """Match line against PATTERNS. Return (event_type, named_groups) or None."""
    for name, pat in PATTERNS.items():
        m = pat.search(line)
        if m:
            return name, m.groupdict()
    return None


def emit(event: dict, out: TextIO) -> None:
    out.write(json.dumps(event) + "\n")
    out.flush()


def supervise(
    log_path: Path,
    out: TextIO = sys.stdout,
    notify: bool = True,
    stuck_threshold: float = STUCK_THRESHOLD_SECONDS,
) -> None:
    """Main loop. Returns only when 'completion' is seen or the log file disappears."""
    state = SupervisorState.fresh()

    for line in tail_lines(log_path):
        # Stuck check (fires when tail returns None and we've been idle too long).
        # "Stuck" means the log file itself has stopped growing — a true hang.
        # Phases like baseline generation don't emit sample_cards but do produce
        # plenty of routine httpx/INFO log activity, so we treat ANY line as a
        # heartbeat. last_progress_at is updated on every line read below.
        if line is None:
            idle = time.time() - state.last_progress_at
            if idle > stuck_threshold:
                event = {"event": "stuck", "idle_seconds": int(idle), "log": str(log_path)}
                emit(event, out)
                if notify:
                    macos_notify("Sweep stuck", f"No log activity in {int(idle / 60)} min")
                state.last_progress_at = time.time()  # reset to avoid re-firing every poll
            continue

        # Any incoming log line counts as evidence the harness is alive.
        state.last_progress_at = time.time()

        match = classify(line)
        if not match:
            continue
        kind, groups = match

        # Track current model so we can attribute errors to the right (model, corpus) bucket
        if kind == "model_activate":
            state.current_model = groups.get("model")

        if kind == "sample_card":
            state.last_progress_at = time.time()
            sample_model = groups.get("model", "")
            if sample_model:
                state.consecutive_errors[sample_model] = 0
            event = {"event": "sample_card", **groups, "line": line[:300]}
            emit(event, out)

        elif kind == "phase_boundary":
            phase = int(groups["phase"])
            state.last_phase_seen = phase
            state.last_progress_at = time.time()
            event = {"event": "phase_boundary", "phase": phase, "line": line.strip()}
            emit(event, out)
            if notify:
                macos_notify(f"Phase {phase} complete", line.strip())

        elif kind == "completion":
            event = {"event": "completion", "line": line.strip()}
            emit(event, out)
            if notify:
                macos_notify("Sweep complete", line.strip())
            return

        elif kind == "blocked":
            event = {"event": "blocked", "line": line.strip()}
            emit(event, out)
            if notify:
                macos_notify("Sweep blocked", line.strip()[:80])
            return

        elif kind == "rate_limit":
            event = {"event": "rate_limit", "line": line.strip()}
            emit(event, out)
            if notify:
                macos_notify("API rate limit", "Anthropic rate limit hit; harness will retry")

        elif kind == "error":
            # Attribute to current model (best-effort; some errors arrive before any
            # model is active, e.g. during corpus ingest — those aren't counted toward
            # any streak).
            model = state.current_model or ""
            if model:
                state.consecutive_errors[model] += 1
                count = state.consecutive_errors[model]
            else:
                count = 0
            event = {
                "event": "error",
                "model": state.current_model,
                "consecutive": count,
                "line": line.strip()[:300],
            }
            emit(event, out)
            if model and count >= CONSECUTIVE_ERROR_THRESHOLD:
                skip_event = {
                    "event": "skip_recommendation",
                    "model": model,
                    "reason": f"{count} consecutive errors",
                }
                emit(skip_event, out)
                if notify:
                    macos_notify(
                        "Skip recommended",
                        f"{model}: {count} errors in a row",
                    )
                # Reset the counter so we only recommend skip once per streak
                state.consecutive_errors[model] = 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="supervisor")
    p.add_argument("--log-file", required=True, type=Path)
    p.add_argument("--no-notify", action="store_true")
    p.add_argument(
        "--stuck-threshold-seconds",
        type=int,
        default=STUCK_THRESHOLD_SECONDS,
        help="Idle time before flagging as stuck",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if not args.log_file.exists():
        print(f"log file not found: {args.log_file}", file=sys.stderr)
        return 2
    supervise(args.log_file, notify=not args.no_notify, stuck_threshold=args.stuck_threshold_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
