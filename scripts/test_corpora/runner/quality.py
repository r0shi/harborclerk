"""Answer/baseline quality checks for the sweep.

The sweep's old ``status == "completed" and answer != ""`` predicate was
generous to a fault: it counted answers like *"I'm sorry, but I don't have
the capability to search the specific documents"* as DONE, because the chat
endpoint returned cleanly and the answer was non-empty. The result is a
matrix in which a model's "100% pass" can really mean "100% punted."

This module provides three independent quality probes:

- ``classify_answer`` — buckets a model answer as real / refusal / roleplay /
  empty so the sweep can demote DONE → DEGRADED with a specific reason.
- ``find_unfilled_placeholder`` — flags a question that still contains a
  ``{{contract_a}}``-style template marker (it should have been substituted
  during sampling). Such questions cannot pass and must not be sent to the
  LLM.
- ``baseline_quality_problem`` — flags a baseline answer that is itself
  unusable (corpus was empty at baseline-gen time, Claude saw a placeholder
  and asked to clarify, etc.) so the sweep doesn't compute meaningless
  citation/entity metrics against it.

All three return ``None`` (or ``"real"``) on the happy path and a short
human-readable string when something is off, so they're easy to log into
``metrics.csv`` and ``log.txt`` without further wrapping.
"""

from __future__ import annotations

import re
from typing import Literal

# ── classify_answer ──

# Short, polite "I can't help" answers that signal the model didn't try to
# engage with the corpus. Match case-insensitively. Each phrase is something
# that appeared verbatim in the 2026-05-05-prod sweep across phi4-mini,
# smollm3-3b, etc., so the list is empirically grounded — not preemptive.
_REFUSAL_PHRASES = (
    "i'm sorry, but i don't have",
    "i don't have access to",
    "i don't have the capability",
    "i cannot access",
    "i cannot retrieve",
    "i haven't been trained on specific documents",
    "i'm an ai language model",
    "i'm an ai assistant",
    "i don't have the ability to access",
    "i am unable to access",
    "i am not able to access",
)

# Tool-call syntax leaking into the answer body. Small models with weak
# tool-calling fine-tunes sometimes narrate tool calls as text instead of
# emitting them as structured calls. The bracketed-tool-name pattern is the
# fingerprint: ``[search_documents: "..."]`` rather than an actual call.
_TOOL_NAMES_FOR_ROLEPLAY = (
    "search_documents",
    "kb_search",
    "kb_batch_search",
    "batch_search",
    "read_passages",
    "kb_read_passages",
    "expand_context",
    "kb_expand_context",
    "get_document",
    "kb_get_document",
    "list_documents",
    "kb_list_recent",
    "corpus_overview",
    "kb_corpus_overview",
    "entity_search",
    "kb_entity_search",
    "find_related",
    "kb_find_related",
    "document_outline",
    "kb_document_outline",
)
_ROLEPLAY_RE = re.compile(
    r"\[\s*(?:" + "|".join(_TOOL_NAMES_FOR_ROLEPLAY) + r")\b",
    re.IGNORECASE,
)

# Cap on answer length for refusal detection. Above this we trust that any
# refusal phrase appearing in a long answer is being quoted, not asserted.
# 1500 chars is roughly two paragraphs of real content; refusals tend to be
# under 800.
_REFUSAL_MAX_LEN = 1500

AnswerLabel = Literal["real", "refusal", "roleplay", "empty"]


def classify_answer(answer: str | None) -> tuple[AnswerLabel, str | None]:
    """Bucket a model answer into one of four labels.

    Returns ``(label, reason)``. ``reason`` is a short human string when
    ``label`` is not ``"real"`` — suitable for the metrics CSV ``status``
    column or a log line.

    Detection order matters:

    1. Empty/whitespace → ``empty``. This is the dead-LLM cascade signature
       (chat returns "completed" with no tokens in ~2s) and is already
       captured as DEGRADED upstream; included here so the function totals
       to a complete classification.
    2. Roleplay → ``roleplay``. Checked before refusal because a model that
       roleplays tool calls *and* hedges should be flagged as roleplay (the
       more specific bucket).
    3. Refusal → ``refusal``, but only when the answer is short. Long
       answers that happen to quote a refusal phrase are real content.
    4. Anything else → ``real``.
    """
    if not answer or not answer.strip():
        return ("empty", "completed with empty answer")

    if _ROLEPLAY_RE.search(answer):
        return (
            "roleplay",
            "completed with tool roleplay (model emitted tool-call syntax as text)",
        )

    if len(answer) <= _REFUSAL_MAX_LEN:
        low = answer.lower()
        for phrase in _REFUSAL_PHRASES:
            if phrase in low:
                return ("refusal", f"completed with refusal (matched: {phrase!r})")

    return ("real", None)


# ── placeholder detection ──

# Matches ``{{name}}``, ``{{ contract_a }}``, etc. Greedy is fine — we're
# looking for the existence of a placeholder, not parsing nested ones.
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")


def find_unfilled_placeholder(text: str) -> str | None:
    """Return the first ``{{...}}`` placeholder in ``text``, or ``None``.

    Used to reject questions like ``"What is the governing law of the
    {{contract_a}} agreement?"`` that should have been substituted during
    sampling but weren't. Sending such a question to an LLM produces either
    a clarification request or a literal-search attempt — neither is a
    pass.
    """
    if not text:
        return None
    m = _PLACEHOLDER_RE.search(text)
    return m.group(0) if m else None


# ── baseline_quality_problem ──

# Baseline-answer fingerprints that indicate the baseline itself is unusable
# as a gold standard. These come from re-reading the 2026-05-05-prod
# baselines: Claude saw either an empty corpus or an unfilled placeholder
# and answered accordingly. Computing citation_overlap or entity_overlap
# against such answers tells you nothing about the model under test.
_BASELINE_EMPTY_CORPUS_SIGNATURES = (
    "corpus appears to be",  # "the corpus appears to be completely empty"
    "no documents have been uploaded",
    "no documents have been ingested",
    "knowledge base is empty",
    # Added 2026-05-18: phrasings Sonnet emitted in 2026-05-05-prod when the
    # corpus was empty at baseline-gen time that the original list missed.
    # 30 baselines (12 cuad, 18 synthetic) sailed through the old detector
    # because their wording happened to differ by a word ("is currently empty"
    # vs "is empty", "currently contains no documents" vs "no documents",
    # etc.). After bolding normalization below, these substrings catch them.
    "knowledge base is currently empty",
    "knowledge base is currently completely empty",
    "knowledge base currently contains no documents",
    "knowledge base appears to be empty",
    "knowledge base appears to be completely empty",
    "knowledge base appears empty",
    "document corpus is currently empty",
    "document corpus is currently completely empty",
    "corpus is currently empty",
    "no documents currently ingested",
    "no documents ingested in the",
    # French — the synthetic corpus has French questions, and Sonnet answers
    # in French when asked in French. "actuellement vide" is the French
    # equivalent of "currently empty" and is specific enough to the
    # empty-corpus signal that it's safe even without a cited-count check.
    "actuellement vide",
)
_BASELINE_NOTHING_FOUND_SIGNATURES = (
    "i was unable to find",
    "i couldn't find any",
    "all searches returned zero results",
    "no information about",
)
_BASELINE_PLACEHOLDER_SIGNATURES = (
    "unfilled template placeholder",
    "i notice your message contains",
    "looks like the specific contract name or identifier wasn't filled",
)
_BASELINE_REFUSAL_SIGNATURES = (
    "i don't have the capability to search",
    "i don't have access to external databases",
)


def _normalize_for_match(text: str) -> str:
    """Lowercase + strip markdown bold/italic asterisks. Sonnet wraps
    individual words like ``**empty**`` mid-sentence, which would otherwise
    prevent a substring match against "is currently empty". The asterisks
    carry no semantic load — they're a presentation artifact — so removing
    them before matching is safe and makes the marker list short."""
    return text.lower().replace("*", "")


def baseline_quality_problem(baseline: dict) -> str | None:
    """Return a reason if the baseline answer is unusable as a gold standard.

    The sweep should still record the unit's operational status and write
    the row to ``metrics.csv``, but with empty quality columns and a logged
    skip — there's no point computing ``citation_overlap`` against an
    "I can't find anything" baseline.

    Returns ``None`` when the baseline looks usable.
    """
    if not isinstance(baseline, dict):
        return "baseline payload is not a dict"

    answer = (baseline.get("answer") or "").strip()
    if not answer:
        return "baseline answer is empty"

    low = _normalize_for_match(answer)

    for sig in _BASELINE_EMPTY_CORPUS_SIGNATURES:
        if sig in low:
            return "baseline says corpus is empty"
    for sig in _BASELINE_PLACEHOLDER_SIGNATURES:
        if sig in low:
            return "baseline saw unfilled placeholder"
    for sig in _BASELINE_NOTHING_FOUND_SIGNATURES:
        if sig in low:
            return "baseline found no matching documents"
    for sig in _BASELINE_REFUSAL_SIGNATURES:
        if sig in low:
            return "baseline is a refusal"

    return None
