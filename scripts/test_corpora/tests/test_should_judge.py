"""Tests for the ``_should_judge`` predicate in sweep.py.

The judge decision is one of the few branches in the sweep that directly
controls Anthropic spend (Sonnet 4.6 is the judge model). Bugs here are
costly in two directions — silently dropping judgments leaves the matrix
quality-blind, silently judging too much burns credits — so the predicate
gets its own unit tests.
"""

from __future__ import annotations

import pytest

from scripts.test_corpora.runner.state import Status
from scripts.test_corpora.runner.sweep import _should_judge

# ── phase gating ──


@pytest.mark.parametrize("phase", [0, 1, 2, 3, 6])
def test_judge_skipped_for_non_target_phases(phase: int) -> None:
    assert not _should_judge(
        phase=phase,
        status=Status.DONE,
        model_answer="A real answer.",
        no_judge=False,
    )


@pytest.mark.parametrize("phase", [4, 5])
def test_judge_runs_for_phase_4_and_5(phase: int) -> None:
    assert _should_judge(
        phase=phase,
        status=Status.DONE,
        model_answer="A real answer.",
        no_judge=False,
    )


# ── status gating ──


@pytest.mark.parametrize("status", [Status.PENDING, Status.IN_PROGRESS, Status.DEGRADED, Status.ERROR])
def test_judge_skipped_for_non_done_status(status: Status) -> None:
    assert not _should_judge(
        phase=4,
        status=status,
        model_answer="A real answer.",
        no_judge=False,
    )


# ── empty answer ──


@pytest.mark.parametrize("answer", ["", "   ", "\n\n", "\t"])
def test_judge_skipped_for_empty_or_whitespace_answer(answer: str) -> None:
    # The dead-LLM cascade signature: chat returns "completed" with no tokens
    # in 2 seconds. We don't want to burn Sonnet calls on these.
    assert not _should_judge(
        phase=4,
        status=Status.DONE,
        model_answer=answer,
        no_judge=False,
    )


# ── opt-out ──


def test_judge_skipped_when_no_judge_flag_set() -> None:
    assert not _should_judge(
        phase=4,
        status=Status.DONE,
        model_answer="A real answer.",
        no_judge=True,
    )


def test_judge_skipped_when_no_judge_flag_set_phase_5() -> None:
    assert not _should_judge(
        phase=5,
        status=Status.DONE,
        model_answer="A real answer.",
        no_judge=True,
    )


# ── happy path ──


def test_judge_runs_when_all_conditions_met() -> None:
    assert _should_judge(
        phase=4,
        status=Status.DONE,
        model_answer="Real answer with content.",
        no_judge=False,
    )
