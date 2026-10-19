"""Which failures of a retried background loop deserve a WARNING (#593).

Logging every failure floods the log while the database is down: one traceback
per tick, for an hour. Logging only the first failure of a streak hides what
comes next. A transient "connection refused" can turn into a persistent
"password authentication failed", and nothing at WARNING would say so while
folder changes or new mail labels silently stop taking effect.

So a failure is loud when it starts a streak, when its error differs from the
previous one, and on every `heartbeat_every`-th failure. Everything else is quiet.

"Differs" compares a stable key, not the message: SQLAlchemy's message embeds the
statement and its parameters (uuids), so every failure of the same kind would
read as a new one, each with a traceback. A database error is keyed by its
driver error's type and SQLSTATE, or by the driver's own message when it has no
SQLSTATE (a connection refused, an authentication failure); any other error by
its type and message.
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
        signature = _signature(exc)
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


def _signature(exc: BaseException) -> tuple[type, str]:
    driver = getattr(exc, "orig", None) or exc
    pgcode = getattr(driver, "pgcode", None)
    if pgcode:
        return (type(driver), str(pgcode))
    return (type(driver), str(driver))
