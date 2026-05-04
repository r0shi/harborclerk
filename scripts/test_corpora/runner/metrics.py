"""Pure metric functions. No I/O.

Citation overlap is at doc_id level (chunk-level is too noisy because
hybrid retrieval pulls slightly different chunks per run). Entity overlap
uses spaCy NER over the answer text; the same models Harbor Clerk uses
(``en_core_web_sm``, ``fr_core_news_sm``).
"""

from __future__ import annotations

import functools
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
