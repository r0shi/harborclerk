"""Thin wrapper around spaCy NER with lazy per-language loading and graceful fallback.

Models are loaded on first use and cached per-process. The cache stores
``None`` for languages whose model couldn't be loaded — callers fall back
to no-NER for chunks in that language rather than raising. Translation:
French entities won't be extracted at all if the French pack isn't
installed (and English isn't a meaningful fallback for French names);
better to report no entities than wrong entities.

For non-English models we look in two places:

1. The standard Python import path — works if the wheel was ``pip
   install``'d (legacy and bundled-en behaviour).
2. The extracted lang-pack directory at ``<lang_packs>/<lang>/ner/<package>/``
   — the path that ``download_artifact`` populates for opt-in languages.

Behaviour during a model swap (e.g. operator removed French via the API):
the cached nlp object stays loaded for the worker's lifetime. Worker
restart picks up the change. Documented in
``project_language_packs_on_demand.md`` as a known limitation.
"""

import logging
import threading
from typing import NamedTuple

logger = logging.getLogger(__name__)


class EntitySpan(NamedTuple):
    text: str
    type: str
    start_char: int
    end_char: int


# Chunk language strings (set by chunk.py via langdetect) -> ISO 639-1.
# Languages not in this map fall through to English.
_CHUNK_LANGUAGE_TO_ISO = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "dutch": "nl",
    "portuguese": "pt",
}

# ISO 639-1 -> spaCy package name. Mirrors the install_subpath segments
# in harbor_clerk/languages.py — adding a language there means adding
# an entry here too.
_ISO_TO_SPACY_PACKAGE = {
    "en": "en_core_web_sm",  # bundled
    "fr": "fr_core_news_sm",
    "de": "de_core_news_sm",
    "es": "es_core_news_sm",
    "it": "it_core_news_sm",
    "nl": "nl_core_news_sm",
    "pt": "pt_core_news_sm",
}


# Cache loaded spaCy models per ISO code. Stores None for languages that
# we tried and failed to load (avoids retrying repeatedly per chunk).
_nlp_cache: dict[str, object | None] = {}
_nlp_cache_lock = threading.Lock()
_spacy_available: bool | None = None


def is_ner_available() -> bool:
    """True if spaCy and the bundled English model are importable.

    Used by the pipeline orchestrator (advance_pipeline) to decide
    whether to skip the entities stage entirely. If even English NER
    can't load, the stage has nothing to do.
    """
    global _spacy_available
    if _spacy_available is not None:
        return _spacy_available
    try:
        import spacy  # noqa: F401

        spacy.load("en_core_web_sm")
        _spacy_available = True
    except Exception:
        _spacy_available = False
        logger.info("spaCy NER not available — entities stage will be skipped")
    return _spacy_available


def _try_load_for_iso(iso: str):
    """Attempt to load a spaCy model for the given ISO code.

    Returns the loaded ``nlp`` object on success, ``None`` on any failure
    (no package, extracted dir missing, spaCy import error, etc.).
    """
    package = _ISO_TO_SPACY_PACKAGE.get(iso)
    if package is None:
        return None

    try:
        import spacy
    except Exception:
        logger.exception("spaCy is not importable; NER unavailable")
        return None

    # 1. Standard import path (works for pip-installed models including
    #    the bundled en_core_web_sm).
    try:
        nlp = spacy.load(package)
        logger.info("Loaded spaCy model %s (system path)", package)
        return nlp
    except Exception:
        # Not pip-installed. Fall through to lang-pack extracted dir.
        pass

    # 2. Lang-pack extracted dir (populated by download_artifact for
    #    opt-in language packs).
    from harbor_clerk.lang_packs.storage import lang_dir

    extracted = lang_dir(iso) / "ner" / package
    if not extracted.is_dir():
        logger.warning(
            "spaCy model %s not found on system path or at %s — install via "
            "/api/languages/%s/install (Tool.NER) or pip install %s",
            package,
            extracted,
            iso,
            package,
        )
        return None

    try:
        nlp = spacy.load(str(extracted))
        logger.info("Loaded spaCy model %s from lang-pack at %s", package, extracted)
        return nlp
    except Exception:
        logger.exception("Failed to load spaCy model from %s", extracted)
        return None


def _get_nlp(language: str):
    """Get or lazily load the spaCy model for a chunk-language string.

    ``language`` is the value chunk.py wrote to ``chunks.language`` — one
    of the keys in ``_CHUNK_LANGUAGE_TO_ISO``. Unknown values resolve to
    English. Returns ``None`` if no model is available; callers must skip
    NER for those chunks.
    """
    iso = _CHUNK_LANGUAGE_TO_ISO.get(language, "en")

    # Fast path — already cached (positive or negative)
    if iso in _nlp_cache:
        return _nlp_cache[iso]

    with _nlp_cache_lock:
        if iso in _nlp_cache:
            return _nlp_cache[iso]
        nlp = _try_load_for_iso(iso)
        _nlp_cache[iso] = nlp
        return nlp


def extract_entities(text: str, language: str = "english") -> list[EntitySpan]:
    """Extract named entities from a single text. Returns ``[]`` if no
    spaCy model is available for the language."""
    if not text:
        return []
    nlp = _get_nlp(language)
    if nlp is None:
        return []
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
    """Batch-extract entities, grouping by language for ``nlp.pipe()``
    efficiency. Chunks in languages with no available model get ``[]``.

    Args:
        chunks: list of ``(text, language)`` tuples

    Returns:
        list of entity lists, one per input chunk, in input order
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
        if nlp is None:
            logger.info(
                "NER: no spaCy model for language %r — skipping %d chunks",
                language,
                len(items),
            )
            continue
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


def _reset_nlp_cache_for_tests() -> None:
    """Test helper — clear the per-language nlp cache so a fresh attempt
    runs after the test fixture changes which models are on disk."""
    with _nlp_cache_lock:
        _nlp_cache.clear()
