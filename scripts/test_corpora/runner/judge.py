"""LLM-as-judge client, either vendor.

Sends ``(question, baseline_answer, model_answer)`` to the judge model with the rubric below and returns a
structured verdict. The rubric is intentionally narrow: fact-level coverage and correctness, not prose quality.

The verdict follows ``answers_question``, not ``completeness``. It used to follow completeness (implicitly: the
prompt named no rule), and under that rubric a correct, short answer scored "marginal" beside a reference that also
quoted the clause and cited the page: 20 of 39 answers CUAD's own annotations marked right, for Haiku 4.5, and one
right answer in seven for Sonnet 4.6. Adding this one dimension and binding the verdict to it took both judges'
agreement with the annotations from 0.89 to 0.96 and 0.97 (docs/reports/2026-09-22-rubric-test.md). Three other
changes tried in the same test did nothing or made the judges stricter. Completeness is still scored and reported.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

from scripts.test_corpora.runner import spend
from scripts.test_corpora.runner.providers.factory import _ANTHROPIC_PREFIXES, _OPENAI_PREFIXES

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
- answers_question: does the model answer correctly answer what the question asked? Do not penalise
  brevity, or the absence of detail the question did not ask for. Penalise wrong facts, contradictions of
  the baseline, and not answering (including saying nothing was found when the baseline found it).

Return JSON only (no prose, no markdown fences):
{{
  "claim_recall": int,
  "claim_precision": int,
  "entity_recall": int,
  "completeness": int,
  "answers_question": int,
  "missing_facts": ["..."],
  "extra_facts": ["..."],
  "contradictions": ["..."],
  "verdict": "pass" | "marginal" | "fail"
}}
The verdict follows answers_question alone, not completeness.
"""


@dataclasses.dataclass
class JudgeVerdict:
    claim_recall: int
    claim_precision: int
    entity_recall: int
    completeness: int
    answers_question: int
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


# Claude models that think by themselves when `thinking` is left out. A judge that thinks is another candidate,
# and at 1,500 output tokens it would be cut off mid-thought.
_THINKS_BY_DEFAULT = ("claude-sonnet-5", "claude-opus-5")


def vendor_of(model: str) -> str:
    """ "anthropic" or "openai" by model id, the same prefixes the baseline providers route by. A local model
    (`gpt-oss-*`) is neither, and neither is anything else: a judge must be a cloud model."""
    from scripts.test_corpora.runner.providers.factory import _LOCAL_OVERRIDES

    if model.startswith(_ANTHROPIC_PREFIXES):
        return "anthropic"
    if model.startswith(_OPENAI_PREFIXES) and not model.startswith(_LOCAL_OVERRIDES):
        return "openai"
    raise ValueError(f"{model!r} is neither a Claude nor an OpenAI model")


class JudgeClient:
    """One rubric, either vendor. `client` is a metered client of the vendor `model` belongs to; left out, one is
    built for it. Both vendors' calls are booked as `judge`."""

    def __init__(self, client: Any | None = None, model: str | None = None):
        from scripts.test_corpora import conftest as cfg

        self._model = model or cfg.JUDGE_MODEL
        self._vendor = vendor_of(self._model)
        self._client = client or (
            spend.anthropic_client("judge") if self._vendor == "anthropic" else spend.openai_client("judge")
        )

    def _ask(self, prompt: str) -> str:
        if self._vendor == "anthropic":
            extra = {"thinking": {"type": "disabled"}} if self._model.startswith(_THINKS_BY_DEFAULT) else {}
            msg = self._client.messages.create(
                model=self._model, max_tokens=1500, messages=[{"role": "user", "content": prompt}], **extra
            )
            return next((b.text for b in msg.content if isinstance(getattr(b, "text", None), str)), "")
        resp = self._client.chat.completions.create(
            model=self._model, max_completion_tokens=4000, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content or ""

    def judge(self, question: str, baseline: str, model_answer: str) -> JudgeVerdict:
        prompt = JUDGE_PROMPT.format(question=question, baseline=baseline, model_answer=model_answer)
        text = self._ask(prompt)
        spend.get_meter().count_unit("judge")
        data = _extract_json(text)
        return JudgeVerdict(
            claim_recall=int(data["claim_recall"]),
            claim_precision=int(data["claim_precision"]),
            entity_recall=int(data["entity_recall"]),
            completeness=int(data["completeness"]),
            answers_question=int(data["answers_question"]),
            missing_facts=data.get("missing_facts", []),
            extra_facts=data.get("extra_facts", []),
            contradictions=data.get("contradictions", []),
            verdict=data["verdict"],
        )
