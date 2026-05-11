"""Tests for runner.quality — answer/baseline/placeholder validation."""

from __future__ import annotations

import pytest

from scripts.test_corpora.runner.quality import (
    baseline_quality_problem,
    classify_answer,
    find_unfilled_placeholder,
)

# ── classify_answer ──


def test_empty_string_is_empty() -> None:
    label, reason = classify_answer("")
    assert label == "empty"
    assert reason == "completed with empty answer"


@pytest.mark.parametrize("answer", [" ", "\n", "\t\n  \t"])
def test_whitespace_only_is_empty(answer: str) -> None:
    label, _ = classify_answer(answer)
    assert label == "empty"


def test_none_is_empty() -> None:
    label, _ = classify_answer(None)
    assert label == "empty"


def test_refusal_short_answer() -> None:
    # Verbatim from phi4-mini cuad-ask-1 in 2026-05-05-prod.
    answer = (
        "I'm sorry, but I don't have the capability to search the specific "
        "documents or retrieve information from them. My responses are based "
        "on a mixture of licensed data."
    )
    label, reason = classify_answer(answer)
    assert label == "refusal"
    assert reason is not None
    assert "refusal" in reason


def test_refusal_long_answer_treated_as_real() -> None:
    # A genuine answer that happens to quote a refusal phrase shouldn't be
    # flagged. We cap the refusal-detector to short answers (<= 1500 chars).
    quoted = (
        "The contract states the parties may terminate with 30 days notice. "
        "Note: an earlier draft included the boilerplate clause "
        "'I'm sorry, but I don't have access to external databases' which "
        "was later struck. "
    )
    real_content = "More substantive content. " * 100  # push it past 1500 chars
    label, _ = classify_answer(quoted + real_content)
    assert label == "real"


def test_roleplay_bracketed_search_documents() -> None:
    # Verbatim from phi4-mini cuad-ask-4 in 2026-05-05-prod.
    answer = '[search_documents: "California law contracts"]  [search_documents: "California contract law"]'
    label, reason = classify_answer(answer)
    assert label == "roleplay"
    assert reason is not None
    assert "roleplay" in reason


def test_roleplay_bracketed_kb_search() -> None:
    answer = "[kb_search: query='contracts']"
    label, _ = classify_answer(answer)
    assert label == "roleplay"


def test_roleplay_with_whitespace_around_tool_name() -> None:
    answer = "[ search_documents: 'foo' ]"
    label, _ = classify_answer(answer)
    assert label == "roleplay"


def test_roleplay_beats_refusal_when_both_present() -> None:
    # An answer that both roleplays AND apologizes should be classified as
    # roleplay (the more specific failure mode).
    answer = "I'm sorry, but I don't have the capability. [search_documents: 'foo']"
    label, _ = classify_answer(answer)
    assert label == "roleplay"


def test_real_answer_passes() -> None:
    answer = (
        "The termination notice period in the Vendor Agreement is 30 days. "
        "Page 4, section 9.1. [VendorAgreement.pdf, page 4]"
    )
    label, reason = classify_answer(answer)
    assert label == "real"
    assert reason is None


def test_real_answer_with_brackets_but_no_tool_name() -> None:
    # Quoting "[California law]" or similar must not match the roleplay
    # regex — only known tool names with the bracket prefix do.
    answer = "The contract was governed by [California law] and exclusively."
    label, _ = classify_answer(answer)
    assert label == "real"


# ── find_unfilled_placeholder ──


def test_unfilled_placeholder_found() -> None:
    text = "What is the governing law of the {{contract_a}} agreement?"
    assert find_unfilled_placeholder(text) == "{{contract_a}}"


def test_unfilled_placeholder_with_spaces() -> None:
    text = "List parties to the {{ contract_b }} agreement"
    assert find_unfilled_placeholder(text) == "{{ contract_b }}"


def test_no_placeholder_returns_none() -> None:
    assert find_unfilled_placeholder("What is the governing law?") is None


def test_empty_text_returns_none() -> None:
    assert find_unfilled_placeholder("") is None
    assert find_unfilled_placeholder(None) is None  # type: ignore[arg-type]


def test_single_brace_does_not_match() -> None:
    # A single brace like {foo} or }foo{ should not be treated as a
    # placeholder — only the double-brace Jinja-style form.
    assert find_unfilled_placeholder("Show {foo}") is None
    assert find_unfilled_placeholder("Show }contract_a{") is None


def test_only_first_placeholder_returned() -> None:
    text = "Compare {{contract_a}} with {{contract_b}}"
    assert find_unfilled_placeholder(text) == "{{contract_a}}"


# ── baseline_quality_problem ──


def test_baseline_empty_answer() -> None:
    assert baseline_quality_problem({"answer": ""}) == "baseline answer is empty"
    assert baseline_quality_problem({"answer": "   "}) == "baseline answer is empty"
    assert baseline_quality_problem({}) == "baseline answer is empty"


def test_baseline_not_a_dict() -> None:
    assert baseline_quality_problem("not a dict") is not None  # type: ignore[arg-type]


def test_baseline_says_corpus_empty() -> None:
    # Verbatim from cuad-research-1 baseline in 2026-05-05-prod.
    baseline = {
        "answer": (
            "The corpus appears to be **completely empty** — there are "
            "currently **no documents** ingested into the knowledge base."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_saw_placeholder() -> None:
    # Verbatim from cuad-ask-1 baseline in 2026-05-05-prod.
    baseline = {"answer": ("I notice your message contains an unfilled template placeholder: **`{{contract_a}}`**.")}
    assert baseline_quality_problem(baseline) == "baseline saw unfilled placeholder"


def test_baseline_found_nothing() -> None:
    # Verbatim from synthetic-ask-1 baseline.
    baseline = {
        "answer": (
            "I was unable to find any information about a Q3 vendor contract "
            "with Globex Supplies in the knowledge base."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline found no matching documents"


def test_baseline_real_answer_passes() -> None:
    baseline = {
        "answer": (
            "The termination notice period in the Vendor Agreement is 30 days. "
            "This is consistent with industry practice for service agreements."
        )
    }
    assert baseline_quality_problem(baseline) is None
