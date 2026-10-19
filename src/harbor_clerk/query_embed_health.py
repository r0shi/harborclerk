"""The API process's evidence about the embedder, and the verdict health reports.

Two sources feed it: search's own query embeds (search._embed_query) and the
background health probe (api/routes/system.py). `/system/health` reports
`verdict()` and, when the evidence is stale, starts a probe; once the evidence
is past EMBEDDER_MAX_EVIDENCE_AGE_SECONDS it waits for that probe, briefly
(api/routes/system.py `_embedder_check`).

Evidence is one of three kinds, and the latest event wins:

- **success**: an embed returned a vector.
- **failure**: a connection failure, a 5xx, or an invalid vector. Degraded.
- **busy**: a timeout. The embedder serves one encode at a time, so during an
  ingest a probe or a query can queue behind 64-text batches; that is the
  embedder working, so busy reads "ok", with a note saying for how long.
  Timeouts alone never make the verdict an error, however long they last:
  telling a wedged slot from a saturated one by timing broke twice (phantom
  streaks, then a real three-worker ingest read as "not answering" while
  every batch succeeded). Only failures do, which is the #685 case.

A 4xx (the input was rejected) says nothing about the service and is not
recorded. In-process and best-effort: a restart forgets it, and the probe
rebuilds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from harbor_clerk.error_text import HEALTH_DETAIL_LIMIT

# How stale the evidence may get before /system/health starts a background
# probe (api/routes/system.py reads it from here).
EMBEDDER_PROBE_TTL_SECONDS = 30.0

# Busy events further apart than this are not one streak (the "busy for N s"
# in the note). Twice the probe TTL: while the embedder is being watched,
# consecutive probes land within it.
BUSY_STREAK_MAX_GAP_SECONDS = 2 * EMBEDDER_PROBE_TTL_SECONDS

# Past this age a verdict is not reported at all: health says "checking" (with
# the age) until a fresh probe lands. An hour-old "ok" for a dead embedder is
# worse than no answer.
EMBEDDER_MAX_EVIDENCE_AGE_SECONDS = 2 * EMBEDDER_PROBE_TTL_SECONDS

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
    # Bounded here, for every caller: the verdict is published on the
    # unauthenticated /system/health, and an exception's text can carry far
    # more than its type (see error_text.HEALTH_DETAIL_LIMIT).
    if len(detail) > HEALTH_DETAIL_LIMIT:
        detail = detail[:HEALTH_DETAIL_LIMIT].rstrip() + "…"
    _evidence.kind, _evidence.at, _evidence.detail, _evidence.busy_since = "failure", _now(), detail, None


def record_busy() -> None:
    now = _now()
    # A streak is consecutive busy evidence. A gap longer than
    # BUSY_STREAK_MAX_GAP_SECONDS since the previous event starts a new one:
    # two isolated timeouts an hour apart are not an hour of being busy.
    gap = now - _evidence.at if _evidence.at is not None else None
    if (
        _evidence.kind != "busy"
        or _evidence.busy_since is None
        or (gap is not None and gap > BUSY_STREAK_MAX_GAP_SECONDS)
    ):
        _evidence.busy_since = now
    _evidence.kind, _evidence.at, _evidence.detail = "busy", now, ""


def classify_embed_error(exc: BaseException) -> str:
    """ "busy", "rejected" or "failure" for an exception from embedder_client.

    The client raises its unretried errors from the httpx exception that caused
    them. A read or write timeout means the request landed and the embedder is
    still working on it: busy. An HTTP 4xx is the input rejected. Everything
    else is a failure, including a connect or pool timeout: those are retried
    and then raised by `_give_up` with no cause, and they mean the embedder
    could not be reached at all, which is unavailable, not busy.
    """
    cause = exc.__cause__
    if isinstance(exc, TimeoutError) or isinstance(cause, (httpx.ReadTimeout, httpx.WriteTimeout)):
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
    # Busy is healthy however long it lasts; the note says for how long.
    streak = (_evidence.at or 0.0) - (_evidence.busy_since or 0.0)
    if streak >= 1:
        return "ok", f"{BUSY_NOTE}; busy for {streak:.0f}s"
    return "ok", BUSY_NOTE


def reset() -> None:
    """Forget all evidence (tests)."""
    global _evidence
    _evidence = _Evidence()
