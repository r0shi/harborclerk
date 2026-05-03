"""Tests for per-language NER loading and graceful fallback.

Patches ``_try_load_for_iso`` rather than driving real spaCy loads — the
goal is to validate the cache + fallback contract, not spaCy itself.
The bundled English model is exercised via the existing test suite's
extract_entities_batch coverage.
"""

import pytest

from harbor_clerk.worker import ner as ner_mod
from harbor_clerk.worker.ner import (
    _CHUNK_LANGUAGE_TO_ISO,
    _ISO_TO_SPACY_PACKAGE,
    EntitySpan,
    _reset_nlp_cache_for_tests,
    extract_entities_batch,
)


@pytest.fixture(autouse=True)
def _clear_ner_cache():
    """Each test gets a fresh per-language cache so prior loads don't
    bleed across."""
    _reset_nlp_cache_for_tests()
    yield
    _reset_nlp_cache_for_tests()


class _FakeNlp:
    """Minimal spaCy-shaped object — enough to exercise extract_entities_batch."""

    def __init__(self, entities_per_text):
        self._entities = entities_per_text

    def __call__(self, text):
        return _FakeDoc(self._entities.get(text, []))

    def pipe(self, texts):
        for t in texts:
            yield _FakeDoc(self._entities.get(t, []))


class _FakeDoc:
    def __init__(self, entities):
        self.ents = [_FakeEnt(*e) for e in entities]


class _FakeEnt:
    def __init__(self, text, label, start, end):
        self.text = text
        self.label_ = label
        self.start_char = start
        self.end_char = end


def test_chunk_language_to_iso_covers_all_known_languages():
    """Every chunk-language string we'd plausibly see must map to ISO."""
    assert _CHUNK_LANGUAGE_TO_ISO["english"] == "en"
    assert _CHUNK_LANGUAGE_TO_ISO["french"] == "fr"
    assert _CHUNK_LANGUAGE_TO_ISO["german"] == "de"
    assert _CHUNK_LANGUAGE_TO_ISO["spanish"] == "es"
    assert _CHUNK_LANGUAGE_TO_ISO["italian"] == "it"
    assert _CHUNK_LANGUAGE_TO_ISO["dutch"] == "nl"
    assert _CHUNK_LANGUAGE_TO_ISO["portuguese"] == "pt"


def test_iso_to_spacy_package_covers_all_chunk_languages():
    """Every ISO code we map a chunk language to must have a spaCy package."""
    for chunk_lang, iso in _CHUNK_LANGUAGE_TO_ISO.items():
        assert iso in _ISO_TO_SPACY_PACKAGE, f"{chunk_lang!r} maps to ISO {iso!r} with no spaCy package"


def test_iso_to_spacy_package_matches_languages_static_map():
    """The chunk-language ISO codes plus the languages.py LANGUAGES dict
    should agree on supported languages — adding to one without the
    other leaves a half-implemented language."""
    from harbor_clerk.languages import LANGUAGES

    for code in LANGUAGES:
        assert code in _ISO_TO_SPACY_PACKAGE, (
            f"languages.py defines {code!r} but worker/ner.py has no _ISO_TO_SPACY_PACKAGE entry"
        )


def test_extract_entities_batch_returns_per_language_results(monkeypatch):
    """Two languages, two fake models — each chunk's entities come from
    the correct language's model in input order."""
    en_nlp = _FakeNlp({"smith works at acme": [("Smith", "PERSON", 0, 5), ("acme", "ORG", 15, 19)]})
    fr_nlp = _FakeNlp({"dubois habite paris": [("Dubois", "PERSON", 0, 6), ("Paris", "GPE", 14, 19)]})

    def fake_load(iso):
        return {"en": en_nlp, "fr": fr_nlp}.get(iso)

    monkeypatch.setattr(ner_mod, "_try_load_for_iso", fake_load)

    chunks = [
        ("smith works at acme", "english"),
        ("dubois habite paris", "french"),
    ]
    results = extract_entities_batch(chunks)

    assert len(results) == 2
    assert results[0][0] == EntitySpan(text="Smith", type="PERSON", start_char=0, end_char=5)
    assert results[1][0] == EntitySpan(text="Dubois", type="PERSON", start_char=0, end_char=6)


def test_extract_entities_batch_skips_chunks_when_model_unavailable(monkeypatch):
    """If a language's model can't be loaded, those chunks get [] but
    other languages' chunks still process normally — no exception."""
    en_nlp = _FakeNlp({"hello world": [("hello", "GREETING", 0, 5)]})

    def fake_load(iso):
        return en_nlp if iso == "en" else None

    monkeypatch.setattr(ner_mod, "_try_load_for_iso", fake_load)

    chunks = [
        ("hello world", "english"),
        ("bonjour monde", "french"),
        ("hello world", "english"),
    ]
    results = extract_entities_batch(chunks)

    assert len(results) == 3
    assert results[0] == [EntitySpan(text="hello", type="GREETING", start_char=0, end_char=5)]
    assert results[1] == []
    assert results[2] == [EntitySpan(text="hello", type="GREETING", start_char=0, end_char=5)]


def test_failed_loads_are_cached_to_avoid_retry_storm(monkeypatch):
    """If loading French fails once, subsequent _get_nlp("french") calls
    must not retry — that would log + filesystem-probe per chunk on a
    100-chunk doc."""
    call_count = 0

    def counting_load(iso):
        nonlocal call_count
        call_count += 1
        return None

    monkeypatch.setattr(ner_mod, "_try_load_for_iso", counting_load)

    chunks = [("text", "french") for _ in range(10)]
    extract_entities_batch(chunks)
    extract_entities_batch(chunks)
    assert call_count == 1, f"_try_load_for_iso called {call_count} times; expected 1"


def test_extract_entities_batch_handles_empty_input():
    assert extract_entities_batch([]) == []


def test_unknown_chunk_language_falls_back_to_english(monkeypatch):
    """A chunk with a language string we don't know (e.g. 'klingon')
    should be processed via the English model rather than dropped."""
    en_nlp = _FakeNlp({"text": [("ent", "MISC", 0, 3)]})

    def fake_load(iso):
        return en_nlp if iso == "en" else None

    monkeypatch.setattr(ner_mod, "_try_load_for_iso", fake_load)

    results = extract_entities_batch([("text", "klingon")])
    assert results[0] == [EntitySpan(text="ent", type="MISC", start_char=0, end_char=3)]
