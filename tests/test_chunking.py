"""Tests for chunking helpers: _split_text, _find_page_range, _detect_language."""

from unittest.mock import patch

from harbor_clerk.worker.stages.chunk import (
    _detect_language,
    _find_code_fence_ranges,
    _find_heading_positions_in_text,
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


# --- _find_code_fence_ranges ---


def test_fence_ranges_empty_text():
    assert _find_code_fence_ranges("") == []


def test_fence_ranges_no_fence():
    text = "Just prose.\n\nSome more prose.\n"
    assert _find_code_fence_ranges(text) == []


def test_fence_ranges_single_fence():
    text = "Intro.\n\n```python\ncode_line()\n```\n\nOutro.\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1
    start, end = ranges[0]
    # Opening fence starts at char 8 (after "Intro.\n\n").
    assert text[start:].startswith("```python")
    # End is the start of the line after the closing fence (i.e. start of "\n").
    assert text[start:end].endswith("```\n")


def test_fence_ranges_multiple_fences():
    text = "```\nA\n```\n\nMid.\n\n```py\nB\n```\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 2
    # Each range covers exactly one fence.
    for s, e in ranges:
        block = text[s:e]
        assert block.count("```") == 2  # opening + closing


def test_fence_ranges_tilde_fence():
    """``~~~`` fences (CommonMark alternative) are also recognized."""
    text = "Intro.\n\n~~~\ncode\n~~~\n\nOutro.\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1


def test_fence_ranges_unterminated():
    """A fence that never closes extends to end-of-text."""
    text = "Intro.\n\n```\nstart of code\nmore code\n"
    ranges = _find_code_fence_ranges(text)
    assert len(ranges) == 1
    start, end = ranges[0]
    assert end == len(text)


# --- _find_heading_positions_in_text ---


def test_heading_positions_empty():
    assert _find_heading_positions_in_text("", []) == []
    assert _find_heading_positions_in_text("text", []) == []
    assert _find_heading_positions_in_text("", [{"title": "X"}]) == []


def test_heading_positions_at_start():
    text = "Heading One\n\nBody text.\n"
    headings = [{"title": "Heading One"}]
    assert _find_heading_positions_in_text(text, headings) == [0]


def test_heading_positions_multiple_in_order():
    text = "Section A\n\nProse here.\n\nSection B\n\nMore prose.\n"
    headings = [{"title": "Section A"}, {"title": "Section B"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 2
    assert out[0] < out[1]
    assert text[out[0] :].startswith("Section A")
    assert text[out[1] :].startswith("Section B")


def test_heading_positions_skips_prose_occurrence():
    """Title appearing in prose (not at line start) is NOT matched."""
    text = "Refers to the Budget section.\n\nBudget\n\nActual content.\n"
    headings = [{"title": "Budget"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    # Position must be at the line-start occurrence (after the first \n\n),
    # not the prose occurrence at offset 14.
    assert text[out[0] :].startswith("Budget")
    if out[0] > 0:
        assert text[out[0] - 1] == "\n"


def test_heading_positions_low_water_keeps_order():
    """Repeated titles are matched in document order via low-water mark."""
    text = "Notes\n\nDetail.\n\nNotes\n\nMore.\n"
    headings = [{"title": "Notes"}, {"title": "Notes"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 2
    assert out[0] < out[1]


def test_heading_positions_skips_missing_title():
    """A heading whose title can't be located is skipped (not in output)."""
    text = "Real Heading\n\nBody.\n"
    headings = [{"title": "Real Heading"}, {"title": "Not Present"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    assert text[out[0] :].startswith("Real Heading")


def test_heading_positions_skips_empty_title():
    """A heading with an empty title is skipped without error."""
    text = "Section\n\nBody.\n"
    headings = [{"title": ""}, {"title": "Section"}]
    out = _find_heading_positions_in_text(text, headings)
    assert len(out) == 1
    assert text[out[0] :].startswith("Section")


# --- _split_text heading-aware ---


def test_split_text_prefers_heading_boundary():
    """A heading position in the upper half of the target window becomes the break."""
    pre = "p" * 600
    heading_line = "\nHeading B\n"
    rest = "x" * 600
    text = pre + heading_line + rest
    heading_pos = len(pre) + 1  # char of 'H' in "Heading B" (after the '\n')
    result = _split_text(
        text,
        target=900,
        overlap=50,
        heading_positions=[heading_pos],
    )
    assert len(result) >= 2
    assert result[0][1] == heading_pos


def test_split_text_ignores_heading_below_half_target():
    """A heading in the first half of the target window is NOT preferred."""
    text = "Heading\n" + "x" * 1500
    result = _split_text(text, target=1000, overlap=50, heading_positions=[0])
    # The first chunk MUST NOT end at offset 0 (degenerate empty chunk).
    assert result[0][1] > 0


def test_split_text_no_heading_falls_back_to_paragraph():
    """With no headings in window, the existing paragraph/sentence/word fallback runs."""
    para1 = "A" * 600
    para2 = "B" * 600
    text = para1 + "\n\n" + para2
    result = _split_text(text, target=800, overlap=50, heading_positions=[])
    assert len(result) >= 2
    first_end = result[0][1]
    # Paragraph break is at pos 602 (after the \n\n).
    assert abs(first_end - 602) <= 10


# --- _split_text fence-protected ---


def test_split_text_does_not_break_inside_code_fence():
    """If the proposed chunk end falls inside a fence range, push past the fence."""
    pre = "p" * 800
    fence = "```\n" + ("L\n" * 100) + "```\n"  # ~210 chars
    text = pre + fence + ("x" * 200)
    fence_start = len(pre)
    fence_end = len(pre) + len(fence)
    result = _split_text(
        text,
        target=900,
        overlap=50,
        code_fence_ranges=[(fence_start, fence_end)],
    )
    first_end = result[0][1]
    assert not (fence_start < first_end < fence_end), (
        f"chunk ends at {first_end}, inside fence [{fence_start}, {fence_end})"
    )


def test_split_text_fence_protection_with_heading():
    """Heading break and fence protection compose: a heading just past the fence wins."""
    pre = "p" * 600
    fence = "```\ncode\n```\n"
    rest = "Heading\n" + "x" * 400
    text = pre + fence + rest
    fence_start = len(pre)
    fence_end = len(pre) + len(fence)
    heading_pos = fence_end  # right after the fence
    result = _split_text(
        text,
        target=800,
        overlap=50,
        heading_positions=[heading_pos],
        code_fence_ranges=[(fence_start, fence_end)],
    )
    assert result[0][1] == heading_pos


def test_split_text_backward_compat_no_params():
    """Calling _split_text without the new parameters keeps existing behavior."""
    text = "word " * 300
    result_old = _split_text(text, target=500, overlap=50)
    result_new = _split_text(text, target=500, overlap=50, heading_positions=None, code_fence_ranges=None)
    assert result_old == result_new
