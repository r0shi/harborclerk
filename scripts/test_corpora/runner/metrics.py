"""Pure metric functions. No I/O.

Citation overlap is at doc_id level (chunk-level is too noisy because
hybrid retrieval pulls slightly different chunks per run). Entity overlap
uses spaCy NER over the answer text; the same models Harbor Clerk uses
(``en_core_web_sm``, ``fr_core_news_sm``).

The retrieval-eval metrics (``recall_at_k``, ``reciprocal_rank``,
``ndcg_at_k``) treat baseline ``cited_doc_ids`` as binary relevance labels
and grade a ranked list of doc_ids returned by ``/api/search``. They are
single-query; aggregate at the call site (the mean of ``reciprocal_rank``
across queries is MRR).
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable


@functools.lru_cache(maxsize=2)
def _load_spacy(lang: str):
    import spacy

    if lang == "en":
        return spacy.load("en_core_web_sm")
    if lang == "fr":
        return spacy.load("fr_core_news_sm")
    raise ValueError(f"unsupported lang: {lang}")


def _entities(text: str, lang: str) -> set[str]:
    if not text.strip():
        return set()
    nlp = _load_spacy(lang)
    doc = nlp(text)
    return {ent.text for ent in doc.ents}


def citation_overlap(baseline_doc_ids: Iterable[str], model_doc_ids: Iterable[str]) -> float:
    """Recall against baseline: |baseline ∩ model| / |baseline|."""
    baseline = set(baseline_doc_ids)
    if not baseline:
        return 0.0
    model = set(model_doc_ids)
    return len(baseline & model) / len(baseline)


def citation_extra(baseline_doc_ids: Iterable[str], model_doc_ids: Iterable[str]) -> int:
    """Count of citations in model not in baseline. Not necessarily bad."""
    return len(set(model_doc_ids) - set(baseline_doc_ids))


def entity_overlap(baseline_text: str, model_text: str, lang: str = "en") -> float:
    """Recall of named entities: |baseline ∩ model| / |baseline|."""
    baseline = _entities(baseline_text, lang)
    if not baseline:
        return 0.0
    model = _entities(model_text, lang)
    # Case-insensitive match on entity surface form
    baseline_norm = {e.lower() for e in baseline}
    model_norm = {e.lower() for e in model}
    return len(baseline_norm & model_norm) / len(baseline_norm)


def recall_at_k(baseline_doc_ids: Iterable[str], ranked_doc_ids: Iterable[str], k: int) -> float:
    """Recall@K: fraction of baseline ids appearing in the first K ranked ids.

    Duplicates in ``ranked_doc_ids`` count once. Returns 0.0 when ``baseline``
    is empty (no truth → no signal) or ``k <= 0``.
    """
    baseline = set(baseline_doc_ids)
    if not baseline or k <= 0:
        return 0.0
    top_k = set(list(ranked_doc_ids)[:k])
    return len(baseline & top_k) / len(baseline)


def reciprocal_rank(baseline_doc_ids: Iterable[str], ranked_doc_ids: Iterable[str]) -> float:
    """1 / (1-indexed rank of the earliest baseline id in the ranked list).

    Returns 0.0 if no baseline id appears in ``ranked_doc_ids`` or either
    input is empty. Caller computes MRR by averaging across queries.
    """
    baseline = set(baseline_doc_ids)
    if not baseline:
        return 0.0
    for i, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id in baseline:
            return 1.0 / i
    return 0.0


def ndcg_at_k(baseline_doc_ids: Iterable[str], ranked_doc_ids: Iterable[str], k: int) -> float:
    """nDCG@K with binary relevance from ``baseline_doc_ids``.

    DCG@K = Σ rel_i / log2(i+1), 1-indexed. IDCG@K is the ideal DCG capped
    at ``min(k, |baseline|)``. Returns 0.0 when baseline is empty, ranked
    is empty, or ``k <= 0``.
    """
    baseline = set(baseline_doc_ids)
    if not baseline or k <= 0:
        return 0.0

    top_k = list(ranked_doc_ids)[:k]
    dcg = 0.0
    for i, doc_id in enumerate(top_k, start=1):
        if doc_id in baseline:
            dcg += 1.0 / math.log2(i + 1)

    ideal_hits = min(k, len(baseline))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return dcg / idcg
