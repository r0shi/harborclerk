"""Tests for the harness's resilience to Anthropic HTTP 529 (Overloaded).

An Anthropic overload can persist for many minutes — far longer than the
SDK's built-in ~5s retry window. Before the ``_with_overload_retry`` guard,
an uncaught ``OverloadedError`` from a Phase 1 baseline call killed the
whole multi-hour sweep. These tests pin down the backoff + give-up
behaviour by simulating a 529 from the Anthropic client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from anthropic._exceptions import OverloadedError

from scripts.test_corpora.runner.sweep import (
    OVERLOAD_RETRY_BUDGET_SECONDS,
    OVERLOAD_RETRY_MAX_DELAY_SECONDS,
    _phase1_baseline,
    _with_overload_retry,
)


def _make_overloaded_error() -> OverloadedError:
    """Build the exact exception the Anthropic SDK raises on HTTP 529."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(529, request=request)
    return OverloadedError("Overloaded", response=response, body=None)


def test_with_overload_retry_retries_then_succeeds():
    """A transient 529 burst is absorbed: the call is retried with
    exponential backoff and its eventual success is returned."""
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _make_overloaded_error()
        return "baseline-result"

    result = _with_overload_retry(flaky, sleep=sleeps.append)

    assert result == "baseline-result"
    assert len(calls) == 3  # two 529s, then success
    assert sleeps == [60, 120]  # exponential backoff between attempts


def test_with_overload_retry_reraises_after_budget_exhausted():
    """A sustained overload that outlasts the ~1 hr budget re-raises
    OverloadedError so the caller can leave the unit PENDING — but never
    sleeps past the budget, and never a single sleep long enough to trip
    the supervisor's 30-minute 'stuck' alarm."""
    sleeps: list[float] = []

    def always_overloaded():
        raise _make_overloaded_error()

    with pytest.raises(OverloadedError):
        _with_overload_retry(always_overloaded, sleep=sleeps.append)

    assert len(sleeps) >= 5  # retried many times before giving up
    assert sum(sleeps) <= OVERLOAD_RETRY_BUDGET_SECONDS
    assert all(s <= OVERLOAD_RETRY_MAX_DELAY_SECONDS for s in sleeps)


def test_phase1_baseline_retry_recovers_from_anthropic_529(tmp_path):
    """End-to-end: a Phase 1 baseline whose Anthropic client 529s twice
    still produces a written baseline once the overload clears."""
    response = MagicMock(
        content=[MagicMock(text="A thorough answer.")],
        stop_reason="end_turn",
    )
    client = MagicMock()
    client.messages.create.side_effect = [
        _make_overloaded_error(),
        _make_overloaded_error(),
        response,
    ]
    sleeps: list[float] = []

    out = _with_overload_retry(
        _phase1_baseline,
        client,
        None,  # mcp_session — None means no tools, single completion
        "cuad",
        "q1",
        "What are the notice periods?",
        tmp_path,
        sleep=sleeps.append,
    )

    assert out["answer"] == "A thorough answer."
    assert client.messages.create.call_count == 3
    assert sleeps == [60, 120]
    assert (tmp_path / "baselines" / "cuad" / "q1.json").exists()
