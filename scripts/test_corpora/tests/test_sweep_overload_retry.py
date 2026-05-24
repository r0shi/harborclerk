"""Tests for the harness's resilience to transient Anthropic API errors —
HTTP 429 (rate-limited) and HTTP 529 (overloaded).

A rate-limit window or Anthropic overload can persist for many minutes —
far longer than the SDK's built-in ~5s retry window. Before the
``_with_anthropic_retry`` guard, an uncaught ``RateLimitError`` /
``OverloadedError`` from a Phase 1 baseline call killed the whole
multi-hour sweep. These tests pin down the backoff + give-up behaviour by
simulating 429s and 529s from the Anthropic client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from anthropic._exceptions import OverloadedError, RateLimitError

from scripts.test_corpora.runner.sweep import (
    ANTHROPIC_RETRY_BUDGET_SECONDS,
    ANTHROPIC_RETRY_MAX_DELAY_SECONDS,
    _phase1_baseline,
    _with_anthropic_retry,
)


def _make_overloaded_error() -> OverloadedError:
    """Build the exact exception the Anthropic SDK raises on HTTP 529."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(529, request=request)
    return OverloadedError("Overloaded", response=response, body=None)


def _make_rate_limit_error() -> RateLimitError:
    """Build the exact exception the Anthropic SDK raises on HTTP 429."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return RateLimitError("Rate limited", response=response, body=None)


def test_with_anthropic_retry_retries_on_529_then_succeeds():
    """A transient 529 burst is absorbed: the call is retried with
    exponential backoff and its eventual success is returned."""
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _make_overloaded_error()
        return "baseline-result"

    result = _with_anthropic_retry(flaky, sleep=sleeps.append)

    assert result == "baseline-result"
    assert len(calls) == 3  # two 529s, then success
    assert sleeps == [60, 120]  # exponential backoff between attempts


def test_with_anthropic_retry_reraises_529_after_budget_exhausted():
    """A sustained overload that outlasts the ~1 hr budget re-raises
    OverloadedError so the caller can leave the unit PENDING — but never
    sleeps past the budget, and never a single sleep long enough to trip
    the supervisor's 30-minute 'stuck' alarm."""
    sleeps: list[float] = []

    def always_overloaded():
        raise _make_overloaded_error()

    with pytest.raises(OverloadedError):
        _with_anthropic_retry(always_overloaded, sleep=sleeps.append)

    assert len(sleeps) >= 5  # retried many times before giving up
    assert sum(sleeps) <= ANTHROPIC_RETRY_BUDGET_SECONDS
    assert all(s <= ANTHROPIC_RETRY_MAX_DELAY_SECONDS for s in sleeps)


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

    out = _with_anthropic_retry(
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


# ── provider-aware retry (PR-C) ─────────────────────────────────────────────


def _make_openai_rate_limit_error():
    """Build an openai.RateLimitError (HTTP 429)."""
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def _make_openai_api_status_error_503():
    """Build an openai.APIStatusError with status 503 (transient overload)."""
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(503, request=request)
    return openai.APIStatusError("overloaded", response=response, body=None)


def _make_openai_api_status_error_400():
    """Build an openai.APIStatusError with status 400 (bad request) — should
    NOT be retried."""
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.APIStatusError("bad request", response=response, body=None)


def test_retry_handles_openai_rate_limit_error():
    """OpenAI's RateLimitError triggers backoff via _with_provider_retry."""
    from scripts.test_corpora.runner.sweep import _with_provider_retry

    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise _make_openai_rate_limit_error()
        return "ok"

    result = _with_provider_retry(flaky, client_kind="openai", sleep=sleeps.append)
    assert result == "ok"
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_retry_handles_openai_api_status_error_503():
    """OpenAI's APIStatusError 503 (overloaded) triggers backoff."""
    from scripts.test_corpora.runner.sweep import _with_provider_retry

    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise _make_openai_api_status_error_503()
        return "ok"

    result = _with_provider_retry(flaky, client_kind="openai", sleep=sleeps.append)
    assert result == "ok"
    assert len(calls) == 2


def test_retry_does_not_retry_openai_400_bad_request():
    """A 400 from OpenAI is a permanent error — re-raise immediately, don't
    burn the retry budget on it."""
    from scripts.test_corpora.runner.sweep import _with_provider_retry

    sleeps: list[float] = []

    def always_400():
        raise _make_openai_api_status_error_400()

    import openai

    with pytest.raises(openai.APIStatusError):
        _with_provider_retry(always_400, client_kind="openai", sleep=sleeps.append)
    assert sleeps == []  # no retries


def test_retry_with_anthropic_kind_still_works():
    """Back-compat: _with_provider_retry(client_kind='anthropic') matches the
    legacy _with_anthropic_retry behavior."""
    from scripts.test_corpora.runner.sweep import _with_provider_retry

    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise _make_rate_limit_error()
        return "ok"

    result = _with_provider_retry(flaky, client_kind="anthropic", sleep=sleeps.append)
    assert result == "ok"
    assert len(calls) == 2


def test_with_anthropic_retry_legacy_alias_still_callable():
    """The old _with_anthropic_retry name remains as a back-compat alias and
    forwards to _with_provider_retry with client_kind='anthropic'."""
    result = _with_anthropic_retry(lambda: "ok")
    assert result == "ok"


def test_retry_raises_on_unknown_client_kind():
    """Unknown client_kind raises a clear ValueError."""
    from scripts.test_corpora.runner.sweep import _with_provider_retry

    with pytest.raises(ValueError, match="unknown client_kind"):
        _with_provider_retry(lambda: "ok", client_kind="gemini")


def test_with_anthropic_retry_retries_on_429_then_succeeds():
    """A transient 429 rate-limit burst is absorbed exactly like a 529:
    retried with exponential backoff, the eventual success returned. 429s
    usually clear within the SDK's own retry window, but a storm that
    outlasts it would otherwise propagate uncaught and kill the sweep."""
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _make_rate_limit_error()
        return "baseline-result"

    result = _with_anthropic_retry(flaky, sleep=sleeps.append)

    assert result == "baseline-result"
    assert len(calls) == 3  # two 429s, then success
    assert sleeps == [60, 120]  # exponential backoff between attempts


def test_with_anthropic_retry_reraises_429_after_budget_exhausted():
    """A sustained 429 storm that outlasts the ~1 hr budget re-raises
    RateLimitError specifically — the bare ``raise`` preserves the original
    type — so the caller can leave the unit PENDING for a later --resume."""
    sleeps: list[float] = []

    def always_rate_limited():
        raise _make_rate_limit_error()

    with pytest.raises(RateLimitError):
        _with_anthropic_retry(always_rate_limited, sleep=sleeps.append)

    assert len(sleeps) >= 5  # retried many times before giving up
    assert sum(sleeps) <= ANTHROPIC_RETRY_BUDGET_SECONDS
    assert all(s <= ANTHROPIC_RETRY_MAX_DELAY_SECONDS for s in sleeps)


def test_phase1_baseline_retry_recovers_from_anthropic_429(tmp_path):
    """End-to-end: a Phase 1 baseline whose Anthropic client 429s twice
    still produces a written baseline once the rate limit clears."""
    response = MagicMock(
        content=[MagicMock(text="A thorough answer.")],
        stop_reason="end_turn",
    )
    client = MagicMock()
    client.messages.create.side_effect = [
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        response,
    ]
    sleeps: list[float] = []

    out = _with_anthropic_retry(
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
