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
import math
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


# ---------------------------------------------------------------------------
# Task 5 — compare_judges + hand-rolled stats helpers
# ---------------------------------------------------------------------------

_DIMENSIONS = ("correctness", "groundedness", "completeness")
_DELTA_DISAGREEMENT_THRESHOLD = 2


def _ranks(xs: list[float]) -> list[float]:
    """Average rank (1-based). Ties get the mean of their ranks."""
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. Returns 0.0 on n < 2 or zero-variance input."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    mean_x = sum(rx) / len(rx)
    mean_y = sum(ry) / len(ry)
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry))
    if den:
        return num / den
    # Both rank arrays are constant (all ties). Equal inputs → 1.0, otherwise undefined → 0.0.
    return 1.0 if xs == ys else 0.0


def _cohens_kappa(xs: list[int], ys: list[int]) -> float:
    """Cohen's kappa on integer 0-5 scores. Returns 0.0 on n == 0 or
    when both raters used a single bucket (no chance to disagree)."""
    if not xs:
        return 0.0
    n = len(xs)
    observed = sum(1 for a, b in zip(xs, ys) if a == b) / n
    if observed >= 1.0:
        return 1.0  # perfect agreement → kappa = 1 by convention
    # Marginal proportions per category.
    cats = set(xs) | set(ys)
    px = {c: xs.count(c) / n for c in cats}
    py = {c: ys.count(c) / n for c in cats}
    expected = sum(px[c] * py[c] for c in cats)
    # Note: expected >= 1.0 is unreachable here — that case requires both
    # raters using a single identical bucket, which the observed >= 1.0
    # early return above already caught. (When raters use *different* single
    # buckets, expected is 0 because they share no category.)
    return (observed - expected) / (1.0 - expected)


def compare_judges(
    verdicts_a: list[dict],
    verdicts_b: list[dict],
    *,
    judges: tuple[str | None, str | None] = (None, None),
) -> dict:
    """Pure-stats comparison of two judges' verdicts.

    Joins by qid; skips items with judge_error on either side. Returns:
      {n, judges, deltas{dim: {mean, std, min, max}}, spearman{dim: r},
       kappa{dim: k}, disagreements: [...]}

    Delta sign: b - a (judge_b minus judge_a). Documented in the markdown.
    """
    a_by_qid = {v.get("qid"): v for v in verdicts_a if "judge_error" not in v}
    b_by_qid = {v.get("qid"): v for v in verdicts_b if "judge_error" not in v}
    common = sorted(set(a_by_qid) & set(b_by_qid))

    if not common:
        return {
            "n": 0,
            "judges": list(judges),
            "deltas": {},
            "spearman": {},
            "kappa": {},
            "disagreements": [],
        }

    deltas: dict[str, dict] = {}
    spearman: dict[str, float] = {}
    kappa: dict[str, float] = {}

    for dim in _DIMENSIONS:
        a_scores = [int(a_by_qid[q].get(dim, 0)) for q in common]
        b_scores = [int(b_by_qid[q].get(dim, 0)) for q in common]
        diffs = [b - a for a, b in zip(a_scores, b_scores)]
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        deltas[dim] = {
            "mean": round(mean, 4),
            "std": round(math.sqrt(var), 4),
            "min": min(diffs),
            "max": max(diffs),
        }
        spearman[dim] = round(_spearman(a_scores, b_scores), 4)
        kappa[dim] = round(_cohens_kappa(a_scores, b_scores), 4)

    disagreements = []
    for qid in common:
        va, vb = a_by_qid[qid], b_by_qid[qid]
        per_dim_deltas = {dim: abs(int(vb.get(dim, 0)) - int(va.get(dim, 0))) for dim in _DIMENSIONS}
        worst_dim, worst_delta = max(per_dim_deltas.items(), key=lambda kv: kv[1])
        if worst_delta >= _DELTA_DISAGREEMENT_THRESHOLD:
            disagreements.append(
                {
                    "qid": qid,
                    "judge_a": {dim: va.get(dim) for dim in (*_DIMENSIONS, "rationale")},
                    "judge_b": {dim: vb.get(dim) for dim in (*_DIMENSIONS, "rationale")},
                    "max_delta": worst_delta,
                    "delta_dim": worst_dim,
                }
            )

    return {
        "n": len(common),
        "judges": list(judges),
        "deltas": deltas,
        "spearman": spearman,
        "kappa": kappa,
        "disagreements": disagreements,
    }
