"""Tests for the per-model CircuitBreaker."""

from __future__ import annotations

import logging

from scripts.test_corpora.runner.circuit_breaker import CircuitBreaker, is_operational_failure

# ── is_operational_failure ──


def test_none_is_not_operational() -> None:
    assert not is_operational_failure(None)
    assert not is_operational_failure("")


def test_refusal_is_not_operational() -> None:
    # Model declined to engage — real capability signal, not an LLM fault.
    assert not is_operational_failure("completed with refusal (matched: 'i don't have the capability')")


def test_roleplay_is_not_operational() -> None:
    # Small-model fingerprint, real capability signal.
    assert not is_operational_failure("completed with tool roleplay (model emitted tool-call syntax as text)")


def test_unfilled_placeholder_is_not_operational() -> None:
    # Test-data problem, not an LLM/HC fault.
    assert not is_operational_failure("unfilled placeholder: {{contract_a}}")


def test_empty_answer_is_operational() -> None:
    # Dead-LLM cascade signature.
    assert is_operational_failure("completed with empty answer")


def test_interrupted_by_hc_is_operational() -> None:
    assert is_operational_failure("research interrupted by Harbor Clerk")


def test_harness_aborted_is_operational() -> None:
    assert is_operational_failure("harness aborted research: no SSE events for 311s")
    assert is_operational_failure("harness aborted research: research status=interrupted")


def test_status_failed_is_operational() -> None:
    assert is_operational_failure("research finished with status=failed")
    assert is_operational_failure("research finished with status=timeout")


def test_unexpected_result_status_is_operational() -> None:
    assert is_operational_failure("unexpected result status: 'weird'")


# ── CircuitBreaker counters ──


def test_breaker_counters_start_at_zero() -> None:
    b = CircuitBreaker()
    assert b.fail_count("any-model") == 0
    assert b.trip_count("any-model") == 0
    assert not b.should_pause("any-model")
    assert not b.should_skip("any-model")


def test_breaker_increments_on_operational_failure() -> None:
    b = CircuitBreaker(failure_threshold=3)
    b.note("m1", operational_failure=True)
    b.note("m1", operational_failure=True)
    assert b.fail_count("m1") == 2
    assert not b.should_pause("m1")
    b.note("m1", operational_failure=True)
    assert b.fail_count("m1") == 3
    assert b.should_pause("m1")


def test_breaker_resets_on_success() -> None:
    b = CircuitBreaker(failure_threshold=3)
    b.note("m1", operational_failure=True)
    b.note("m1", operational_failure=True)
    b.note("m1", operational_failure=False)  # one good unit
    assert b.fail_count("m1") == 0
    assert not b.should_pause("m1")


def test_breaker_resets_on_non_operational_failure() -> None:
    # A refusal or roleplay is a degraded outcome but the breaker should
    # treat it like a success — it's model behavior, not LLM unhealth.
    b = CircuitBreaker(failure_threshold=3)
    b.note("m1", operational_failure=True)
    b.note("m1", operational_failure=True)
    b.note("m1", operational_failure=False)  # refusal/roleplay counts as reset
    assert b.fail_count("m1") == 0


def test_breaker_is_per_model() -> None:
    b = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        b.note("m1", operational_failure=True)
    assert b.should_pause("m1")
    assert not b.should_pause("m2")


def test_pause_and_reset_resets_fail_count_increments_trip_count() -> None:
    sleeps: list[float] = []
    b = CircuitBreaker(failure_threshold=3, cooldown_seconds=42, sleep=sleeps.append)
    for _ in range(3):
        b.note("m1", operational_failure=True)
    b.pause_and_reset("m1", logging.getLogger("test"))
    assert sleeps == [42]
    assert b.fail_count("m1") == 0
    assert b.trip_count("m1") == 1
    assert not b.should_pause("m1")
    assert not b.should_skip("m1")


def test_breaker_skips_after_max_trips() -> None:
    b = CircuitBreaker(failure_threshold=2, max_trips=3, cooldown_seconds=0, sleep=lambda _: None)
    log = logging.getLogger("test")
    for _ in range(3):
        # Each trip: hit threshold then pause_and_reset.
        b.note("m1", operational_failure=True)
        b.note("m1", operational_failure=True)
        assert b.should_pause("m1")
        b.pause_and_reset("m1", log)
    assert b.trip_count("m1") == 3
    assert b.should_skip("m1")


def test_breaker_default_knobs_match_design() -> None:
    # 3 consecutive failures × 3 trips × 60s = 9 failures + 3min of cooldown
    # before we shed remaining units for a model. Reasonable budget given
    # the 2026-05-05-prod sweep where empty-answer cascades typically hit
    # 10+ units in a row.
    b = CircuitBreaker()
    assert b.failure_threshold == 3
    assert b.max_trips == 3
    assert b.cooldown_seconds == 60
