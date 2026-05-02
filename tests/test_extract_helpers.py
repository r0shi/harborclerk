"""Tests for extract stage helpers: _paginate_text, _alpha_ratio, Tika 422 diagnostics."""

from unittest.mock import patch

import httpx
import pytest

from harbor_clerk.worker.stages.extract import _alpha_ratio, _extract_via_tika, _paginate_text

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


# --- Tika 422 diagnostics ---


def _mock_response(status_code: int, text: str = "", json_data=None) -> httpx.Response:
    """Build a minimal httpx.Response for mocking."""
    request = httpx.Request("PUT", "http://test/tika")
    if json_data is not None:
        import json

        return httpx.Response(status_code, content=json.dumps(json_data).encode(), request=request)
    return httpx.Response(status_code, text=text, request=request)


def test_tika_422_surfaces_pdfbox_container_exception():
    """Real failure mode from production: PDFBox 'Page tree root must be a dictionary'.
    The 422 from /tika has no body; we refetch via /rmeta/text and surface the
    container exception in the raised RuntimeError so it lands in the doc's
    pipeline_status=error message instead of just 'HTTPStatusError 422'.
    """
    rmeta_payload = [
        {
            "X-TIKA:Parsed-By": ["org.apache.tika.parser.pdf.PDFParser"],
            "X-TIKA:EXCEPTION:container_exception": (
                "java.io.IOException: Page tree root must be a dictionary\n"
                "\tat org.apache.pdfbox.pdfparser.COSParser.checkPages(COSParser.java:1416)\n"
                "\tat org.apache.pdfbox.pdfparser.PDFParser.initialParse(PDFParser.java:120)"
            ),
        }
    ]
    responses = [_mock_response(422), _mock_response(200, json_data=rmeta_payload)]

    with (
        patch("httpx.put", side_effect=responses),
        pytest.raises(RuntimeError, match="Page tree root must be a dictionary"),
    ):
        _extract_via_tika(b"%PDF-1.4 ...corrupted...", "application/pdf", is_pdf=True)


def test_tika_422_surfaces_poi_container_exception():
    """Real failure mode from production: POI HWPF 'Index 10 out of bounds for length 7'."""
    rmeta_payload = [
        {
            "X-TIKA:Parsed-By": ["org.apache.tika.parser.microsoft.OfficeParser"],
            "X-TIKA:EXCEPTION:container_exception": (
                "java.lang.IndexOutOfBoundsException: Index 10 out of bounds for length 7\n"
                "\tat java.base/jdk.internal.util.Preconditions.outOfBounds(Preconditions.java:100)"
            ),
        }
    ]
    responses = [_mock_response(422), _mock_response(200, json_data=rmeta_payload)]

    with (
        patch("httpx.put", side_effect=responses),
        pytest.raises(RuntimeError, match="Index 10 out of bounds for length 7"),
    ):
        _extract_via_tika(b"\xd0\xcf\x11\xe0...", "application/msword")


def test_tika_422_with_no_rmeta_detail_still_raises_with_generic_message():
    """If the rmeta refetch also fails or returns no exception, we still raise
    a RuntimeError mentioning Tika rejected the file — the operator at least
    knows it was a 422, not a network error."""
    responses = [_mock_response(422), _mock_response(500, text="rmeta crashed")]

    with patch("httpx.put", side_effect=responses), pytest.raises(RuntimeError, match="Tika rejected file \\(422\\)"):
        _extract_via_tika(b"garbage", "application/pdf")


def test_tika_non_422_failures_still_raise_via_raise_for_status():
    """Make sure we didn't accidentally swallow other status codes — 500 etc.
    should still come through httpx's HTTPStatusError, not get rerouted to rmeta."""
    responses = [_mock_response(500, text="tika crashed")]

    with patch("httpx.put", side_effect=responses), pytest.raises(httpx.HTTPStatusError):
        _extract_via_tika(b"any data", "application/pdf")
