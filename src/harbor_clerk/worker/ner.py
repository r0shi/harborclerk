"""Thin wrapper around spaCy NER with lazy model loading and graceful fallback."""

import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

_nlp_cache: dict[str, object] = {}
_spacy_available: bool | None = None


class EntitySpan(NamedTuple):
    text: str
    type: str
    start_char: int
    end_char: int


def is_ner_available() -> bool:
    """Check if spaCy and at least one model are importable."""
    global _spacy_available
    if _spacy_available is not None:
        return _spacy_available
    try:
        import spacy  # noqa: F401

        # Try loading at least the English model
        spacy.load("en_core_web_sm")
        _spacy_available = True
    except Exception:
        _spacy_available = False
        logger.info("spaCy NER not available — entities stage will be skipped")
    return _spacy_available


def _get_nlp(language: str):
    """Get or lazily load the spaCy model for the given language."""
    model_name = "fr_core_news_sm" if language == "french" else "en_core_web_sm"
    if model_name not in _nlp_cache:
        import spacy

        _nlp_cache[model_name] = spacy.load(model_name)
        logger.info("Loaded spaCy model %s", model_name)
    return _nlp_cache[model_name]


def extract_entities(text: str, language: str = "english") -> list[EntitySpan]:
    """Extract named entities from a single text."""
    if not text:
        return []
    nlp = _get_nlp(language)
    doc = nlp(text)
    return [
        EntitySpan(
            text=ent.text,
            type=ent.label_,
            start_char=ent.start_char,
            end_char=ent.end_char,
        )
        for ent in doc.ents
    ]


def extract_entities_batch(
    chunks: list[tuple[str, str]],
) -> list[list[EntitySpan]]:
    """Batch extract entities using nlp.pipe() for efficiency.

    Args:
        chunks: list of (text, language) tuples

    Returns:
        list of entity lists, one per input chunk
    """
    if not chunks:
        return []

    # Group by language for efficient batching
    by_lang: dict[str, list[tuple[int, str]]] = {}
    for i, (text, language) in enumerate(chunks):
        by_lang.setdefault(language, []).append((i, text))

    results: list[list[EntitySpan]] = [[] for _ in chunks]

    for language, items in by_lang.items():
        nlp = _get_nlp(language)
        indices = [idx for idx, _ in items]
        texts = [text for _, text in items]
        for idx, doc in zip(indices, nlp.pipe(texts)):
            results[idx] = [
                EntitySpan(
                    text=ent.text,
                    type=ent.label_,
                    start_char=ent.start_char,
                    end_char=ent.end_char,
                )
                for ent in doc.ents
            ]

    return results
