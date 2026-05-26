# scripts/test_corpora/runner/cross_judge.py
"""Cross-judge sensitivity: re-judge captures with a second judge model.

Two halves:
  - JudgeProvider Protocol + OpenAIJudgeProvider + rejudge_with(): the
    re-judge driver. JudgeProvider has one method, judge(prompt) -> str,
    so future judges (local model, Gemini, etc.) plug in with a 10-line
    adapter.
  - compare_judges(): pure stats over two verdict lists. Added in Task 5.

Reuses _PROMPT, _PROMPT_FIND, _extract_json, _score from
scripts.test_corpora.runner.answer_judge so the cross-judge rubric tracks
any future change to the Sonnet judge's prompt text.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Protocol, runtime_checkable

from scripts.test_corpora.runner.answer_judge import (
    _PROMPT,
    _PROMPT_FIND,
    _extract_json,
    _score,
)

log = logging.getLogger(__name__)


@runtime_checkable
class JudgeProvider(Protocol):
    """One-shot judge: takes a prompt, returns the raw model response text."""

    def judge(self, prompt: str) -> str: ...


class OpenAIJudgeProvider:
    """OpenAI-backed judge using gpt-* chat completions."""

    def __init__(self, *, model: str = "gpt-4o", client: Any | None = None):
        import openai

        self._client = client or openai.OpenAI()
        self._model = model

    def judge(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=600,
        )
        return resp.choices[0].message.content or ""


def _build_prompt(capture: dict, *, qtype: str, answer_key: Any) -> str:
    """Render the same prompt AnswerJudge.judge_answer would build,
    given the capture + ground-truth answer_key + qtype."""
    cited = "\n".join(f"- {t}" for t in (capture.get("cited_doc_titles") or [])) or "(no passages cited)"
    if qtype == "find":
        ak = answer_key if isinstance(answer_key, dict) else {"count": 0, "all": [], "sample": []}
        sample = ak.get("sample") or []
        rendered_sample = "\n".join(f"- {s}" for s in sample) or "(empty)"
        return _PROMPT_FIND.format(
            question=capture.get("question", ""),
            count=ak.get("count", 0),
            sample_size=len(sample),
            rendered_sample=rendered_sample,
            model_answer=capture.get("answer") or "(empty)",
            cited=cited,
        )
    return _PROMPT.format(
        question=capture.get("question", ""),
        answer_key="NONE" if answer_key is None else answer_key,
        qtype=qtype,
        model_answer=capture.get("answer") or "(empty)",
        cited=cited,
    )


def rejudge_with(
    captures: list[dict],
    judge_provider: JudgeProvider,
    *,
    judge_model: str,
    items: int | None = None,
    seed: int = 42,
    answer_keys: dict[str, Any] | None = None,
    qtypes: dict[str, str] | None = None,
) -> list[dict]:
    """Re-score `captures` (optionally a random sample of `items`) using `judge_provider`.

    Returns a list of verdict dicts, one per captured item attempted:
      {"qid", "correctness", "groundedness", "completeness", "rationale",
       "judge_model", and optionally "judge_error": "..." on provider failure}

    answer_keys: optional dict mapping qid -> ground-truth answer key.
      When absent, the per-capture key falls back to capture["_answer_key"]
      (set by the CLI when loading ground-truth), else None.
    qtypes: optional dict mapping qid -> qtype ("lookup" | "find" | "negative"
      | etc.). Falls back to capture["_qtype"], else "lookup".

    A provider exception on one item is logged + recorded as judge_error
    on that item; remaining items continue.
    """
    answer_keys = answer_keys or {}
    qtypes = qtypes or {}

    if items is not None and items < len(captures):
        rng = random.Random(seed)
        captures = rng.sample(captures, items)

    results: list[dict] = []
    for cap in captures:
        qid = cap.get("question_id", "")
        qtype = qtypes.get(qid) or cap.get("_qtype") or "lookup"
        ak = answer_keys.get(qid) if qid in answer_keys else cap.get("_answer_key")
        prompt = _build_prompt(cap, qtype=qtype, answer_key=ak)
        try:
            raw = judge_provider.judge(prompt)
            data = _extract_json(raw)
            results.append(
                {
                    "qid": qid,
                    "correctness": _score(data, "correctness"),
                    "groundedness": _score(data, "groundedness"),
                    "completeness": _score(data, "completeness"),
                    "rationale": str(data.get("rationale", "")),
                    "judge_model": judge_model,
                }
            )
        except Exception as exc:
            log.warning("rejudge failed for %s: %s", qid, exc)
            results.append(
                {
                    "qid": qid,
                    "judge_model": judge_model,
                    "judge_error": str(exc),
                }
            )
    return results
