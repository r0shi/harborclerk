"""What this process's own query embeds say about the embedder.

Search embeds every query through the embedder, so the API process already
holds the best evidence of whether semantic search works: its last success and
its last failure. `/system/health` reads them (api/routes/system.py) before it
spends an encode on an active probe:

- a query embed that failed, with no success since, inside the health window:
  degraded, even if a probe would pass (the probe's one short text is the
  easiest request the embedder gets);
- a recent success, and no failure since: healthy, no probe needed.

In-process and best-effort by design: a restart forgets it, and the active
probe covers that case.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Evidence:
    last_success: float | None = None
    last_failure: float | None = None
    last_failure_detail: str = ""


_evidence = _Evidence()


def record_query_embed_success() -> None:
    _evidence.last_success = time.monotonic()


def record_query_embed_failure(detail: str) -> None:
    _evidence.last_failure = time.monotonic()
    _evidence.last_failure_detail = detail


def recent_failure(window_seconds: float) -> tuple[float, str] | None:
    """(seconds ago, detail) of a failure inside the window with no success since, else None."""
    failed = _evidence.last_failure
    if failed is None:
        return None
    age = time.monotonic() - failed
    if age >= window_seconds:
        return None
    if _evidence.last_success is not None and _evidence.last_success > failed:
        return None
    return age, _evidence.last_failure_detail


def recent_success(window_seconds: float) -> bool:
    """A success inside the window with no failure since."""
    ok = _evidence.last_success
    if ok is None or time.monotonic() - ok >= window_seconds:
        return False
    return _evidence.last_failure is None or _evidence.last_failure < ok


def reset() -> None:
    """Forget all evidence (tests)."""
    global _evidence
    _evidence = _Evidence()
