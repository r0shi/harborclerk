"""Which failures of a retried background loop deserve a WARNING (#593).

Logging every failure floods the log while the database is down: one traceback
per tick, for an hour. Logging only the first failure of a streak hides what
comes next. A transient "connection refused" can turn into a persistent
"password authentication failed", and nothing at WARNING would say so while
folder changes or new mail labels silently stop taking effect.

So a failure is loud when it starts a streak, when its error differs from the
previous one, and on every `heartbeat_every`-th failure. Everything else is quiet.
"""

from __future__ import annotations

from typing import Literal

HEARTBEAT_EVERY = 10

Loudness = Literal["first", "changed", "heartbeat"] | None


class FailureStreak:
    """Counts the consecutive failures of a retried loop and decides which to log."""

    def __init__(self, heartbeat_every: int = HEARTBEAT_EVERY) -> None:
        self.heartbeat_every = heartbeat_every
        self.failures = 0
        self._last: tuple[type, str] | None = None

    def record(self, exc: BaseException) -> Loudness:
        """Count a failure; return why it should be logged at WARNING, or None."""
        self.failures += 1
        signature = (type(exc), str(exc))
        changed = self._last is not None and signature != self._last
        self._last = signature
        if self.failures == 1:
            return "first"
        if changed:
            return "changed"
        if self.failures % self.heartbeat_every == 0:
            return "heartbeat"
        return None

    def reset(self) -> int:
        """End the streak; return how many failures it had."""
        failures, self.failures, self._last = self.failures, 0, None
        return failures
