"""Unit tests for NER wrapper (no DB required, needs spaCy models installed)."""

import pytest

from harbor_clerk.worker.ner import (
    EntitySpan,
    extract_entities,
    extract_entities_batch,
    is_ner_available,
)


@pytest.fixture(autouse=True)
def _require_spacy():
    """Skip all tests in this module if spaCy models aren't installed."""
    if not is_ner_available():
        pytest.skip("spaCy models not installed")


def test_is_ner_available():
    assert is_ner_available() is True


def test_extract_entities_english():
    text = "Barack Obama visited the United Nations in New York."
    entities = extract_entities(text, "english")
    assert len(entities) > 0
    names = {e.text for e in entities}
    types = {e.type for e in entities}
    # Should find at least a PERSON and a GPE/ORG
    assert "Barack Obama" in names
    assert any(t in types for t in ("PERSON", "ORG", "GPE"))


def test_extract_entities_french():
    text = "Emmanuel Macron a rencontré Angela Merkel à Paris."
    entities = extract_entities(text, "french")
    assert len(entities) > 0
    names = {e.text for e in entities}
    # French model should find at least one person/location
    assert any(n in names for n in ("Emmanuel Macron", "Macron", "Paris", "Angela Merkel", "Merkel"))


def test_extract_entities_empty_text():
    assert extract_entities("", "english") == []
    assert extract_entities("", "french") == []


def test_entity_span_offsets():
    text = "Apple Inc. is in Cupertino."
    entities = extract_entities(text, "english")
    for ent in entities:
        assert isinstance(ent, EntitySpan)
        # Verify offsets match the original text
        assert text[ent.start_char : ent.end_char] == ent.text


def test_batch_extraction():
    chunks = [
        ("Google was founded by Larry Page.", "english"),
        ("Paris est la capitale de la France.", "french"),
        ("", "english"),
    ]
    results = extract_entities_batch(chunks)
    assert len(results) == 3
    # First chunk should have entities
    assert len(results[0]) > 0
    # Second chunk should have entities (French)
    assert len(results[1]) > 0
    # Empty text should have no entities
    assert results[2] == []
