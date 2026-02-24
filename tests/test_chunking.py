"""Tests for chunking helpers: _split_text, _find_page_range, _detect_language."""

from unittest.mock import patch

from harbor_clerk.worker.stages.chunk import (
    _detect_language,
    _find_page_range,
    _split_text,
)


# --- _split_text ---


def test_split_empty():
    assert _split_text("") == []


def test_split_short_text():
    text = "Hello world."
    result = _split_text(text, target=1000, overlap=150)
    assert len(result) == 1
    assert result[0] == (0, len(text))


def test_split_produces_chunks():
    # Generate text longer than one chunk
    text = "word " * 300  # 1500 chars
    result = _split_text(text, target=500, overlap=50)
    assert len(result) > 1


def test_split_full_coverage():
    """Every character should be covered by at least one chunk."""
    text = "word " * 300
    result = _split_text(text, target=500, overlap=50)
    covered = set()
    for start, end in result:
        covered.update(range(start, end))
    assert covered == set(range(len(text)))


def test_split_no_empty_chunks():
    text = "Hello world. This is a test. " * 100
    result = _split_text(text, target=200, overlap=30)
    for start, end in result:
        assert end > start
        assert text[start:end].strip()


def test_split_paragraph_boundary():
    """Chunks should prefer paragraph breaks."""
    para1 = "A" * 600
    para2 = "B" * 600
    text = para1 + "\n\n" + para2
    result = _split_text(text, target=800, overlap=50)
    # First chunk should end at or near the paragraph break
    assert len(result) >= 2
    first_end = result[0][1]
    # Should include the paragraph break (pos 602)
    assert abs(first_end - 602) <= 10


def test_split_overlap():
    """Consecutive chunks should overlap."""
    text = "word " * 500  # 2500 chars
    result = _split_text(text, target=1000, overlap=150)
    if len(result) >= 2:
        # Second chunk should start before first chunk ends
        assert result[1][0] < result[0][1]


# --- _find_page_range ---


def test_find_page_range_single_page():
    offsets = [(1, 0, 1000)]
    assert _find_page_range(100, 500, offsets) == (1, 1)


def test_find_page_range_multi_page():
    offsets = [(1, 0, 500), (2, 500, 1000), (3, 1000, 1500)]
    assert _find_page_range(400, 1100, offsets) == (1, 3)


def test_find_page_range_second_page():
    offsets = [(1, 0, 500), (2, 500, 1000)]
    assert _find_page_range(600, 900, offsets) == (2, 2)


# --- _detect_language ---


def test_detect_english():
    text = "The quick brown fox jumps over the lazy dog. This is a sample English text."
    assert _detect_language(text) == "english"


def test_detect_french():
    text = "Le renard brun rapide saute par-dessus le chien paresseux. Ceci est un texte en français."
    assert _detect_language(text) == "french"


def test_detect_language_fallback_on_error():
    with patch("langdetect.detect", side_effect=Exception("fail")):
        assert _detect_language("anything") == "english"
