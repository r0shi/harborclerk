"""Tests for runner.quality — answer/baseline/placeholder validation."""

from __future__ import annotations

import pytest

from scripts.test_corpora.runner.quality import (
    baseline_quality_problem,
    classify_answer,
    find_unfilled_placeholder,
    nothing_found_signature,
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


def test_the_apps_fallback_for_a_model_that_produced_nothing_is_empty_not_real() -> None:
    # Verbatim from harbor_clerk.llm.chat._nothing_produced_fallback after a time-budget stop (#742). The user
    # received a sentence, but it is the app's, and there is no model answer to judge.
    answer = (
        "_The model produced no answer in the time allowed: the search stopped at its time budget, and when asked "
        "to answer from what it had found, the model wrote nothing in the time it had. Try a narrower question, or "
        "the Research tab for a question that spans many documents._"
    )
    label, reason = classify_answer(answer)
    assert label == "empty"
    assert reason == "completed with the app's fallback (the model produced no answer)"


@pytest.mark.parametrize(
    "answer",
    [
        "_The model produced no answer in the context allowed: the search results filled the model's context._",
        "The model produced no answer after 25 tool rounds: the search stopped at its cap.",
        "  **The model produced no answer: it returned nothing for this question.**",
    ],
)
def test_the_apps_fallback_is_recognised_by_its_opening_whatever_the_reason(answer: str) -> None:
    label, _ = classify_answer(answer)
    assert label == "empty"


def test_the_apps_sentence_for_a_failed_model_server_is_empty_too() -> None:
    answer = (
        "_The model server failed while the final answer was being written (HTTP 500): the search stopped at its "
        "time budget, and the answer then asked for from what it had found did not come. Try again in a moment; if "
        "it fails again, check the model in Settings._"
    )
    label, reason = classify_answer(answer)
    assert label == "empty"
    assert reason == "completed with the app's fallback (the model server failed)"


def test_the_apps_lead_on_a_thought_cut_short_is_empty_too() -> None:
    # Verbatim from harbor_clerk.llm.chat._cut_thought_answer: the thought follows the lead on its own lines.
    answer = (
        "_The model was still reasoning when its time ran out; what it had:_\n\n"
        "The fee is in section 4, which says the vendor bills monthly"
    )
    label, reason = classify_answer(answer)
    assert label == "empty"
    assert reason == "completed with the app's fallback (the model was still reasoning when its time ran out)"
    label, _ = classify_answer(answer.replace("its time", "its context"))
    assert label == "empty"


def test_the_apps_sentence_after_a_watched_prefix_is_still_empty() -> None:
    # Chat keeps the text the user watched arrive in front of the app's sentence; the sentence starts its own line.
    answer = (
        "Let me look. \n\n_The model produced no answer in the time allowed: the search stopped at its time budget, "
        "and when asked to answer from what it had found, the model wrote nothing in the time it had._"
    )
    label, _ = classify_answer(answer)
    assert label == "empty"


def test_a_real_answer_that_mentions_a_model_producing_nothing_is_real() -> None:
    # Only the opening is the app's signature; an answer that says the words later is the model's.
    answer = (
        "The Q3 contract cost $4,500 a month [Globex Services Agreement, page 2]. "
        "On the follow-up the model produced no answer, so that part is unverified; the model server failed once."
    )
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


def test_the_no_findings_list_is_shared_with_the_answer_key_and_two_phrasings_count_only_in_an_opening() -> None:
    """#713: the answer key reads the same list over an answer's opening. The two phrasings added for it are
    ordinary English inside a thorough baseline (review of #764), so they count in an opening and nowhere else:
    a baseline that says so mid-answer is not a no-findings baseline, and its metrics are kept."""
    assert nothing_found_signature("The law is **not explicitly stated** here.", at_opening=True) == (
        "not explicitly stated"
    )
    assert nothing_found_signature("I could not find it.", at_opening=True) == "could not find"
    assert nothing_found_signature("The law is not explicitly stated here.") is None
    assert nothing_found_signature("I was unable to find it.") == "i was unable to find"
    assert nothing_found_signature("Nevada.", at_opening=True) is None and nothing_found_signature("") is None
    thorough = "Nevada law governs (section 12). I could not find a separate venue clause; it is not explicitly stated."
    assert baseline_quality_problem({"answer": thorough}) is None


def test_baseline_real_answer_passes() -> None:
    baseline = {
        "answer": (
            "The termination notice period in the Vendor Agreement is 30 days. "
            "This is consistent with industry practice for service agreements."
        )
    }
    assert baseline_quality_problem(baseline) is None


# ── 2026-05-18 follow-ups: phrasings the original detector missed ──
#
# Each test below uses a verbatim prefix from one of the 30 corrupt baselines
# in 2026-05-05-prod that the detector originally let through, producing
# noise inline metrics + wasted Sonnet judge calls in phase 4. Diagnosed
# while running step 2 of the corrupt-baseline repair.


def test_baseline_says_knowledge_base_is_currently_empty() -> None:
    # Verbatim from synthetic-research-3 and 16 others.
    baseline = {
        "answer": (
            "It appears that the **knowledge base is currently empty** — there are "
            "no documents, chunks, or pages ingested into the corpus at this time."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_currently_empty_with_inline_bold_on_just_the_adjective() -> None:
    # Verbatim from cuad-ask-4..10 — Sonnet bolds just the word "empty"
    # mid-sentence ("currently **empty** — ..."), so a substring match
    # against "knowledge base is currently empty" would fail without
    # markdown normalization.
    baseline = {
        "answer": (
            "It appears that the knowledge base is currently **empty** — there are "
            "no documents, chunks, or pages ingested into the corpus."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_completely_empty() -> None:
    # Verbatim from synthetic-research-1.
    baseline = {
        "answer": (
            "The knowledge base appears to be **completely empty** — it contains "
            "no documents, chunks, or other indexed content."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_appears_to_be_empty() -> None:
    # Verbatim from synthetic-research-4.
    baseline = {
        "answer": ("The knowledge base appears to be empty — there are no documents currently ingested in the corpus.")
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_currently_contains_no_documents() -> None:
    # Verbatim from synthetic-ask-2.
    baseline = {
        "answer": (
            "The knowledge base currently contains **no documents** — it is "
            "completely empty. There are no chunks, pages, or entities indexed."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_document_corpus_is_currently_empty() -> None:
    # Verbatim from synthetic-ask-4, synthetic-research-2.
    baseline = {
        "answer": (
            "It appears that the document corpus is currently **empty** — there "
            "are no documents, pages, or chunks indexed."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_says_corpus_empty_in_french() -> None:
    # Verbatim from synthetic-ask-7 (synthetic has French question variants).
    baseline = {
        "answer": (
            "Il semble que la base de connaissances soit actuellement **vide** — aucun document n'y a été ingéré."
        )
    }
    assert baseline_quality_problem(baseline) == "baseline says corpus is empty"


def test_baseline_real_answer_with_word_empty_still_passes() -> None:
    """Real answers that incidentally mention emptiness — e.g. citing a
    contract clause about empty containers — must not be flagged. The
    markers added in 2026-05-18 are specific enough ("currently empty",
    "appears to be empty", etc.) that a sentence like "the warehouse is
    empty on weekends" shouldn't trip them."""
    baseline = {
        "answer": (
            "Section 4.2 of the agreement states that if the warehouse is empty "
            "at the time of inspection, the inspection is rescheduled. This "
            "clause applies to the South Bay facility specifically."
        )
    }
    assert baseline_quality_problem(baseline) is None


def test_normalize_for_match_strips_asterisks_and_lowercases() -> None:
    """Direct unit test for the normalization helper — covers the cases
    that bit us before normalization (bold-only-around-keyword)."""
    from scripts.test_corpora.runner.quality import _normalize_for_match

    assert _normalize_for_match("**KnowLedge BASE is currently EMPTY**") == "knowledge base is currently empty"
    assert _normalize_for_match("currently **empty** — no docs") == "currently empty — no docs"
    assert _normalize_for_match("plain lowercase passes through") == "plain lowercase passes through"


def test_a_baseline_that_describes_the_corpus_is_not_an_empty_corpus() -> None:
    """The Enron baseline for "what companies are mentioned most often" said "the corpus appears to be the
    well-known Enron email dataset" in the middle of a good answer, and a signature of "corpus appears to be"
    called it empty (bench-20260925-0028-enron: four models' rows without quality columns)."""
    answer = (
        "### 1. **Enron** — 18,127 mentions\nBy a massive margin, **Enron** is the dominant company in the corpus. "
        "This makes complete sense, as the corpus appears to be the well-known **Enron email dataset**, consisting "
        "of internal corporate communications.\n\n### 2. **The New York Times** — 1,129 mentions"
    )
    assert baseline_quality_problem({"answer": answer}) is None
    assert baseline_quality_problem({"answer": "The corpus appears to be empty."}) == "baseline says corpus is empty"


# ── #733: Qwen3's tool-call form ──


def test_roleplay_qwen3_tool_call_form_is_recognised() -> None:
    """Qwen3.6 on the synthetic corpus, once the chat loop stopped offering tools at its context budget: the
    answer ends in the call it wanted to make, in Qwen3's XML-ish form. That is roleplay, not a real answer."""
    answer = (
        'The initial results don\'t mention "Globex Supplies." Let me search more specifically for that vendor '
        "name.\n\n<tool_call>\n<function=search_documents>\n<parameter=query>\nGlobex Supplies vendor contract\n"
        "</parameter>\n<parameter=k>\n10\n</parameter>\n</function>\n</tool_call>"
    )
    label, reason = classify_answer(answer)
    assert label == "roleplay" and reason and "roleplay" in reason


def test_roleplay_function_tag_alone_and_tool_call_tag_alone_each_count() -> None:
    assert (
        classify_answer("Searching now. <function=kb_search> <parameter=query>x</parameter> </function>")[0]
        == "roleplay"
    )
    assert classify_answer('Let me look.\n<tool_call>\n{"name": "search_documents"}\n</tool_call>')[0] == "roleplay"


def test_an_answer_that_mentions_a_function_in_prose_is_not_roleplay() -> None:
    """The fingerprint is the tag, not the word: an answer that talks about a function, or quotes an XML element
    of a document, is real."""
    assert (
        classify_answer("The contract's function=payment clause sets terms at net 30, per the vendor document.")[0]
        == "real"
    )
    assert (
        classify_answer("The policy defines <role>Approver</role> for expense sign-off, effective 2025-06-01.")[0]
        == "real"
    )

    # The tag with a name that is not a tool: a function tag alone is not the fingerprint.
    assert classify_answer("Per the spec, <function=payment> is the clause that sets net-30 terms.")[0] == "real"
    assert classify_answer("Per the spec, <function=search_documents_v2> is a made-up tool.")[0] == "real"
