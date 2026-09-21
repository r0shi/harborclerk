# scripts/test_corpora/runner/providers/base.py
"""Provider Protocol + universal BaselineResult dataclass.

Every concrete provider (AnthropicProvider, OpenAIProvider, ...) exposes the
same `run_question(question, question_id, corpus) -> BaselineResult` method.
BaselineResult is the canonical serialization shape under
`workdir/answer-eval/captures/<corpus>/<model>/<question_id>.json` — same
fields across providers so the judging loop in answer_eval.py is provider-blind.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable

DEFAULT_SYSTEM_PROMPT = """You are answering a user's question about a specific document corpus.
You have access to the corpus only through the provided MCP tools (kb_search,
kb_read_passages, kb_get_document, kb_find_related, etc.). Use them as
needed. Cite specific documents in your answer by doc_id.

Be thorough — your answer is the gold-standard reference for evaluating
local models. Spend tool calls liberally."""


@dataclasses.dataclass
class BaselineResult:
    """Universal return shape from every Provider's `run_question`.

    Fields mirror the pre-refactor `claude_baseline.BaselineResult` so the
    file-on-disk format and downstream consumers (judge, metrics roll-up)
    don't need to change.
    """

    question_id: str
    question: str
    answer: str
    cited_doc_ids: list[str]
    # Stable, ingest-independent identifiers for the cited docs, parallel to
    # cited_doc_ids (same order). doc_id is a random per-ingest UUID, so a
    # baseline's cited_doc_ids never intersect a phase-4 response's doc_ids
    # unless both ran against the exact same ingest. doc_title (the filename
    # stem) is stable across re-ingests, so citation_overlap computed on
    # titles stays correct even when baseline and response come from
    # different ingests. Empty string for any cited doc whose tool result
    # carried no title.
    cited_doc_titles: list[str]
    tool_call_count: int
    # Per-call record [{tool, args, result_summary}] in call order — for
    # groundedness scoring and tool-use auditing.
    tool_transcript: list[dict]
    elapsed_seconds: float
    model: str
    timestamp: str
    # "end_turn" when the model finished on its own; "model_call_limit" when it was still asking for tools at
    # MAX_MODEL_CALLS and the last call was made with tools off. A baseline cut short is a worse reference, and
    # everything scored against it should be able to say so. Defaulted: baselines on disk predate it.
    stopped_by: str = "end_turn"


# Model calls one baseline question may make, the last of them with tools switched off so that it answers from
# what it has. The loop had no bound at all: a question re-sends its whole transcript on every call, so its
# cost grows with the square of the calls, and the run's spend cap was the only thing that would stop it.
# Measured on CUAD, 2026-09-21 (#682): 15 questions took 7.2 calls each on average, the 16th took 24 and cost
# USD 5.98 of a USD 25 run. Twelve is past every question but that one.
MAX_MODEL_CALLS = 12


@runtime_checkable
class Provider(Protocol):
    """The narrow protocol every baseline provider satisfies.

    Implementations: AnthropicProvider, OpenAIProvider. Use
    `providers.factory.make_provider(model, *, mcp_session)` to construct
    one by model name.
    """

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult: ...
