"""Tests for extract stage helpers: _paginate_text, _alpha_ratio."""

from harbor_clerk.worker.stages.extract import _alpha_ratio, _paginate_text

# --- _paginate_text ---


def test_paginate_empty():
    result = _paginate_text("", 3000)
    assert result == [(1, "")]


def test_paginate_short():
    text = "Hello world"
    result = _paginate_text(text, 3000)
    assert result == [(1, text)]


def test_paginate_long():
    text = "A" * 10000
    result = _paginate_text(text, 3000)
    assert len(result) > 1
    # Verify page numbers are sequential 1-based
    for i, (pnum, _) in enumerate(result):
        assert pnum == i + 1


def test_paginate_full_coverage():
    """All text should be covered by pages."""
    text = "word " * 2000  # 10000 chars
    result = _paginate_text(text, 3000)
    reconstructed = "".join(t for _, t in result)
    assert reconstructed == text


def test_paginate_paragraph_boundary():
    """Should prefer breaking at paragraph boundaries."""
    para1 = "A" * 2000
    para2 = "B" * 2000
    text = para1 + "\n\n" + para2
    result = _paginate_text(text, 3000)
    assert len(result) >= 2
    # First page should end at or near the paragraph break
    first_text = result[0][1]
    assert first_text.endswith("\n\n") or len(first_text) <= 3002


# --- _alpha_ratio ---


def test_alpha_ratio_empty():
    assert _alpha_ratio("") == 0.0


def test_alpha_ratio_all_alpha():
    assert _alpha_ratio("abcdef") == 1.0


def test_alpha_ratio_all_digits():
    assert _alpha_ratio("123456") == 0.0


def test_alpha_ratio_mixed():
    ratio = _alpha_ratio("abc123")
    assert 0.4 < ratio < 0.6  # 3/6 = 0.5
