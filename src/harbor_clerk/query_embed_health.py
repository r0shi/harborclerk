"""The API process's evidence about the embedder, and the verdict health reports.

Two sources feed it: search's own query embeds (search._embed_query) and the
background health probe (api/routes/system.py). `/system/health` never waits
on the embedder; it reports `verdict()` and, when the evidence is stale,
starts a probe in the background.

Evidence is one of three kinds, and the latest event wins:

- **success**: an embed returned a vector.
- **failure**: a connection failure, a 5xx, or an invalid vector. Degraded.
- **busy**: a timeout. The embedder serves one encode at a time, so during an
  ingest a probe or a query can queue behind 64-text batches; that is the
  embedder working, so busy reads "ok" with a note. A busy streak with no
  success or failure in between that has lasted EMBEDDER_BUSY_DEGRADED_AFTER_SECONDS
  across consecutive events is no longer busy but wedged: degraded, "not
  answering".

A 4xx (the input was rejected) says nothing about the service and is not
recorded. In-process and best-effort: a restart forgets it, and the probe
rebuilds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

# A busy streak this long (from its first event to its latest) is a wedged
# embedder, not a busy one. Two minutes is many 64-text batches (~3.5 s each
# on an M4) and several probe timeouts in a row: an ingest queue drains
# between them, a wedged encode slot does not.
EMBEDDER_BUSY_DEGRADED_AFTER_SECONDS = 120.0

BUSY_NOTE = "busy: the embedder did not answer in time (it is serving other work)"


def _now() -> float:
    return time.monotonic()


@dataclass
class _Evidence:
    kind: str | None = None  # "success" | "failure" | "busy"
    at: float | None = None
    detail: str = ""
    busy_since: float | None = None


_evidence = _Evidence()


def record_success() -> None:
    _evidence.kind, _evidence.at, _evidence.detail, _evidence.busy_since = "success", _now(), "", None


def record_failure(detail: str) -> None:
    _evidence.kind, _evidence.at, _evidence.detail, _evidence.busy_since = "failure", _now(), detail, None


def record_busy() -> None:
    now = _now()
    if _evidence.kind != "busy" or _evidence.busy_since is None:
        _evidence.busy_since = now
    _evidence.kind, _evidence.at, _evidence.detail = "busy", now, ""


def classify_embed_error(exc: BaseException) -> str:
    """ "busy", "rejected" or "failure" for an exception from embedder_client.

    The client raises its errors from the httpx exception that caused them: a
    read, write or pool timeout is busy; an HTTP 4xx is the input rejected;
    anything else (refused or dropped connection, 5xx, invalid response) is a
    failure.
    """
    cause = exc.__cause__
    if isinstance(exc, TimeoutError) or isinstance(cause, httpx.TimeoutException):
        return "busy"
    if isinstance(cause, httpx.HTTPStatusError) and 400 <= cause.response.status_code < 500:
        return "rejected"
    return "failure"


def record_query_embed_outcome(exc: BaseException | None, detail: str = "") -> None:
    """Record what one of search's query embeds says about the embedder."""
    if exc is None:
        record_success()
        return
    kind = classify_embed_error(exc)
    if kind == "busy":
        record_busy()
    elif kind == "failure":
        record_failure(f"search's query embed failed: {detail}")


def age() -> float | None:
    """Seconds since the latest evidence, or None when there is none."""
    return None if _evidence.at is None else _now() - _evidence.at


def verdict() -> tuple[str, str | None] | None:
    """(status, note) from the latest evidence, however old; None when there is none."""
    kind = _evidence.kind
    if kind is None:
        return None
    if kind == "success":
        return "ok", None
    if kind == "failure":
        return f"error: {_evidence.detail}", None
    streak = (_evidence.at or 0.0) - (_evidence.busy_since or 0.0)
    if streak >= EMBEDDER_BUSY_DEGRADED_AFTER_SECONDS:
        note = f"not answering: every embed has timed out for {streak:.0f}s"
        return f"error: embedder not answering (timed out for {streak:.0f}s)", note
    return "ok", BUSY_NOTE


def reset() -> None:
    """Forget all evidence (tests)."""
    global _evidence
    _evidence = _Evidence()
