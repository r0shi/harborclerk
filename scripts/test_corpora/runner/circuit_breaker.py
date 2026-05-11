"""Per-model circuit breaker for the sweep.

Without this, a dead-LLM cascade (chat returns ``completed`` with no tokens
in ~2 s) fires the next 9+ ask units back-to-back, all empty, all in 2 s
each. The harness records 10+ ``degraded`` rows in 20 seconds while HC is
silently broken. The 2026-05-05-prod sweep had 124 of 386 units (32%) in
this state — overwhelmingly bursts of empty answers immediately after a
model swap or LLM restart.

The breaker tracks consecutive *operational* failures per model. Model
quirks (refusal, tool-call roleplay) don't count — those are real signal
about the model's capability. The breaker fires only for symptoms of an
unhealthy LLM/HC: empty answers, HC-side research interrupts, watchdog
aborts, ``status=failed``.

On trip, it cools the loop down for a fixed period (no fancy recovery
detection — model_status was reporting "ready" while chat returned empty,
so a cooldown is more reliable than a probe). After ``max_trips`` cycles
on the same model, it gives up on that model for the remainder of the
run, leaving remaining units PENDING so ``--resume`` can pick them up
once the operator has investigated.
"""

from __future__ import annotations

import dataclasses
import logging
import time

# These signatures are matched substring-wise against ``error_msg`` strings
# emitted by the sweep's per-unit status assignment. Anything in this list
# implies "the LLM or HC is unhealthy" rather than "the model gave a bad
# answer."
_OPERATIONAL_FAILURE_SIGNATURES = (
    "empty answer",
    "interrupted by harbor clerk",
    "harness aborted research",
    "research finished with status=failed",
    "research finished with status=timeout",
    "unexpected result status",
)


def is_operational_failure(error_msg: str | None) -> bool:
    """Whether ``error_msg`` looks like an LLM/HC fault vs a model fault.

    Returns ``False`` for:

    - ``None`` (clean DONE, no failure).
    - ``"completed with refusal (...)"`` — model declined to engage, real
      capability signal.
    - ``"completed with tool roleplay (...)"`` — small-model tool-call
      fingerprint, real capability signal.
    - ``"unfilled placeholder: {{contract_a}}"`` — test-data problem,
      neither LLM nor HC fault.

    Returns ``True`` for the empty-answer cascades and HC-side interrupts
    that overwhelmingly dominated the 2026-05-05-prod failure modes.
    """
    if not error_msg:
        return False
    low = error_msg.lower()
    return any(sig in low for sig in _OPERATIONAL_FAILURE_SIGNATURES)


@dataclasses.dataclass
class CircuitBreaker:
    """Per-model consecutive-operational-failure tracker with cooldown.

    Default knobs:

    - ``failure_threshold=3`` — after this many consecutive operational
      failures for the same model, pause.
    - ``max_trips=3`` — after this many trip+cooldown cycles for the same
      model, give up and skip remaining units for that model.
    - ``cooldown_seconds=60`` — how long to sleep when the breaker trips.

    Usage in the sweep loop:

    .. code-block:: python

        breaker = CircuitBreaker()
        for u in units:
            if breaker.should_skip(u.model):
                continue  # remaining units stay PENDING for --resume
            if breaker.should_pause(u.model):
                breaker.pause_and_reset(u.model, log)
            # ... run unit, determine final_status + error_msg ...
            breaker.note(
                u.model,
                operational_failure=is_operational_failure(error_msg)
                    if final_status != Status.DONE
                    else False,
            )
    """

    failure_threshold: int = 3
    max_trips: int = 3
    cooldown_seconds: int = 60
    _fail_count: dict[str, int] = dataclasses.field(default_factory=dict)
    _trip_count: dict[str, int] = dataclasses.field(default_factory=dict)
    # Sleep function — overridable for tests. Module-level ``time.sleep`` by
    # default; tests pass a no-op or a recording callable.
    sleep: object = time.sleep

    def note(self, model: str, *, operational_failure: bool) -> None:
        """Record one unit's outcome for this model.

        ``operational_failure=True`` increments the consecutive-failure
        counter; any other outcome (including DONE, refusal, roleplay)
        resets it. The reset on non-operational-failure is important — a
        single real DONE between two empty-answer cascades shouldn't trip
        the breaker.
        """
        if operational_failure:
            self._fail_count[model] = self._fail_count.get(model, 0) + 1
        else:
            self._fail_count[model] = 0

    def should_pause(self, model: str) -> bool:
        """True when ``model`` has hit the consecutive-failure threshold."""
        return self._fail_count.get(model, 0) >= self.failure_threshold

    def should_skip(self, model: str) -> bool:
        """True when ``model`` has tripped+cooled down ``max_trips`` times.

        At this point we've burned ``max_trips × cooldown_seconds`` waiting
        for this model to recover and it hasn't. Remaining units for this
        model are skipped (left PENDING so ``--resume`` can pick them up)
        rather than retried — the operator should investigate.
        """
        return self._trip_count.get(model, 0) >= self.max_trips

    def pause_and_reset(self, model: str, log: logging.Logger) -> None:
        """Sleep for ``cooldown_seconds`` and reset the failure counter.

        Bumps the trip count. Once the trip count reaches ``max_trips``,
        subsequent ``should_skip`` calls return True. The reset is done
        after the sleep so a recovery during the cooldown gets credit on
        the next ``note`` call.
        """
        log.warning(
            "circuit breaker tripped for model=%s after %d consecutive operational failures; sleeping %ds (trip %d/%d)",
            model,
            self._fail_count.get(model, 0),
            self.cooldown_seconds,
            self._trip_count.get(model, 0) + 1,
            self.max_trips,
        )
        self.sleep(self.cooldown_seconds)  # type: ignore[operator]
        self._trip_count[model] = self._trip_count.get(model, 0) + 1
        self._fail_count[model] = 0

    def fail_count(self, model: str) -> int:
        """Read-only view of the current consecutive-failure count. Used by tests."""
        return self._fail_count.get(model, 0)

    def trip_count(self, model: str) -> int:
        """Read-only view of how many times this model has tripped. Used by tests."""
        return self._trip_count.get(model, 0)
