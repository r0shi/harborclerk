"""Sonnet 4.6 LLM-as-judge client.

Sends ``(question, baseline_answer, model_answer)`` to Sonnet 4.6 with
the rubric defined in the design doc and returns a structured verdict.
The rubric is intentionally narrow — only fact-level coverage, not prose
quality.
"""

from __future__ import annotations

import dataclasses
import json
import re

import anthropic


JUDGE_PROMPT = """You are evaluating whether a local LLM's answer reaches the same factual
ground as a Claude baseline answer to the same question.

Question: {question}

Claude baseline answer:
{baseline}

Local model answer:
{model_answer}

Score these dimensions (0–5 each):
- claim_recall: how many factual claims from the baseline appear in the
  model answer (verbatim or paraphrased)?
- claim_precision: are the claims in the model answer supported by the
  baseline (or harmless additions), or are they contradictions?
- entity_recall: does the model answer surface the same named entities
  (people, places, organizations, dates, dollar amounts)?
- completeness: overall coverage of the baseline's territory.

Return JSON only (no prose, no markdown fences):
{{
  "claim_recall": int,
  "claim_precision": int,
  "entity_recall": int,
  "completeness": int,
  "missing_facts": ["..."],
  "extra_facts": ["..."],
  "contradictions": ["..."],
  "verdict": "pass" | "marginal" | "fail"
}}
"""


@dataclasses.dataclass
class JudgeVerdict:
    claim_recall: int
    claim_precision: int
    entity_recall: int
    completeness: int
    missing_facts: list[str]
    extra_facts: list[str]
    contradictions: list[str]
    verdict: str


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the response, even if wrapped in fences."""
    # Try a fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Fall back to the largest curly span
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge response")
    return json.loads(text[start : end + 1])


class JudgeClient:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = "claude-sonnet-4-6"):
        self._client = client or anthropic.Anthropic()
        self._model = model

    def judge(self, question: str, baseline: str, model_answer: str) -> JudgeVerdict:
        prompt = JUDGE_PROMPT.format(question=question, baseline=baseline, model_answer=model_answer)
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        data = _extract_json(text)
        return JudgeVerdict(
            claim_recall=int(data["claim_recall"]),
            claim_precision=int(data["claim_precision"]),
            entity_recall=int(data["entity_recall"]),
            completeness=int(data["completeness"]),
            missing_facts=data.get("missing_facts", []),
            extra_facts=data.get("extra_facts", []),
            contradictions=data.get("contradictions", []),
            verdict=data["verdict"],
        )
