# scripts/test_corpora/runner/answer_judge.py
"""LLM-as-judge for the answer-eval. Scores a model answer against an external
ground-truth key (CUAD expert label) on correctness, groundedness, and
completeness. Independence is not a concern: the judge adjudicates against the
label, it is not itself the source of truth.
"""

from __future__ import annotations

import dataclasses
import json
import re

import anthropic

JUDGE_MODEL = "claude-sonnet-4-6"

_PROMPT = """You are scoring an answer produced by a document-search assistant.

QUESTION:
{question}

GROUND-TRUTH ANSWER KEY (expert-labeled; the source of truth):
{answer_key}

QUESTION TYPE: {qtype}
(For type "negative", the ground-truth answer key is "NONE" — the clause does
not exist in the document, and a correct answer says so.)

THE ASSISTANT'S ANSWER:
{model_answer}

THE PASSAGES THE ASSISTANT CITED:
{cited}

Score three dimensions, each an integer 0-5:
- correctness: does the answer agree with the ground-truth key? (negative type:
  full marks only if it correctly says the clause is absent.)
- groundedness: is every claim supported by a cited passage? 5 = fully
  grounded, 0 = key claims uncited or contradicted by the citation.
- completeness: does the answer cover what the key contains, without burying it
  in irrelevant text?

Reply with ONLY a JSON object:
{{"correctness": <0-5>, "groundedness": <0-5>, "completeness": <0-5>, "rationale": "<one sentence>"}}
"""


@dataclasses.dataclass
class AnswerVerdict:
    correctness: int
    groundedness: int
    completeness: int
    rationale: str


def _extract_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge response")
    return json.loads(text[start : end + 1])


class AnswerJudge:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = JUDGE_MODEL):
        self._client = client or anthropic.Anthropic()
        self._model = model

    def judge_answer(
        self, *, question: str, model_answer: str, cited: str, answer_key: str | None, qtype: str
    ) -> AnswerVerdict:
        prompt = _PROMPT.format(
            question=question,
            answer_key="NONE" if answer_key is None else answer_key,
            qtype=qtype,
            model_answer=model_answer or "(empty)",
            cited=cited or "(no passages cited)",
        )
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(msg.content[0].text)
        return AnswerVerdict(
            correctness=int(data["correctness"]),
            groundedness=int(data["groundedness"]),
            completeness=int(data["completeness"]),
            rationale=str(data.get("rationale", "")),
        )
