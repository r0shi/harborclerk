"""Tests for extract stage helpers: paginate_text, _alpha_ratio, Tika 422 diagnostics."""

from unittest.mock import patch

import httpx
import pytest

from harbor_clerk.worker.stages.extract import (
    _alpha_ratio,
    _extract_headings_via_tika,
    _extract_via_tika,
    _sanitize_external_string,
    is_plain_text_source,
)
from harbor_clerk.worker.text_pagination import paginate_text

# --- paginate_text ---


def test_paginate_empty():
    result = paginate_text("", 3000)
    assert result == [(1, "")]


def test_paginate_short():
    text = "Hello world"
    result = paginate_text(text, 3000)
    assert result == [(1, text)]


def test_paginate_long():
    text = "A" * 10000
    result = paginate_text(text, 3000)
    assert len(result) > 1
    # Verify page numbers are sequential 1-based
    for i, (pnum, _) in enumerate(result):
        assert pnum == i + 1


def test_paginate_full_coverage():
    """All text should be covered by pages."""
    text = "word " * 2000  # 10000 chars
    result = paginate_text(text, 3000)
    reconstructed = "".join(t for _, t in result)
    assert reconstructed == text


def test_paginate_paragraph_boundary():
    """Should prefer breaking at paragraph boundaries."""
    para1 = "A" * 2000
    para2 = "B" * 2000
    text = para1 + "\n\n" + para2
    result = paginate_text(text, 3000)
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


def test_tika_container_exception_non_string_returns_no_detail():
    """Tika sometimes serialises container_exception as a list/array for certain
    exception types (rare but observed). We must not crash trying to .split() it.
    Instead, treat anything non-string as missing and return the standard
    'no container_exception' fallback message."""
    rmeta_payload = [
        {
            "X-TIKA:Parsed-By": ["org.apache.tika.parser.pdf.PDFParser"],
            "X-TIKA:EXCEPTION:container_exception": [
                "first element of an unexpected list",
                "second element",
            ],
        }
    ]
    responses = [_mock_response(422), _mock_response(200, json_data=rmeta_payload)]

    with (
        patch("httpx.put", side_effect=responses),
        pytest.raises(RuntimeError, match="no container_exception"),
    ):
        _extract_via_tika(b"%PDF...", "application/pdf", is_pdf=True)


def test_tika_exception_string_strips_ansi_escapes():
    """A hostile file could cause Tika's Java exception message to embed ANSI
    escape sequences (e.g. \\x1b[31m for red). The diagnostic string is logged
    AND written to the doc's error column — strip them before they land
    anywhere an operator sees them."""
    nasty_exc = "java.io.IOException: \x1b[31mDANGER\x1b[0m \x07bell\nstacktrace line"
    rmeta_payload = [{"X-TIKA:EXCEPTION:container_exception": nasty_exc}]
    responses = [_mock_response(422), _mock_response(200, json_data=rmeta_payload)]

    with patch("httpx.put", side_effect=responses):
        try:
            _extract_via_tika(b"x", "application/pdf", is_pdf=True)
        except RuntimeError as exc:
            msg = str(exc)
            assert "\x1b[31m" not in msg
            assert "\x07" not in msg
            # The actual content survives sans the escapes
            assert "DANGER" in msg
            assert "java.io.IOException" in msg
        else:
            pytest.fail("Expected RuntimeError")


def test_extract_headings_via_tika_logs_422_detail():
    """When the Tika XHTML endpoint returns 422 (same malformed-file scenarios
    as the primary endpoint), the warning should include the underlying parser
    exception, not just the generic 'Heading extraction failed' message.

    Patches the module-level logger directly rather than relying on caplog,
    because other tests in the suite may have reconfigured logging in ways
    that break propagation to caplog's handler.
    """
    rmeta_payload = [
        {
            "X-TIKA:EXCEPTION:container_exception": (
                "java.io.IOException: Page tree root must be a dictionary\n\tat ..."
            ),
        }
    ]
    responses = [_mock_response(422), _mock_response(200, json_data=rmeta_payload)]

    with (
        patch("httpx.put", side_effect=responses),
        patch("harbor_clerk.worker.stages.extract.logger.warning") as mock_warning,
    ):
        result = _extract_headings_via_tika(b"%PDF...", "application/pdf", pages=[(1, "x")])

    assert result == []  # Heading extraction is non-fatal; returns [] on 422
    # The actual exception detail must surface in the warning, not a generic message.
    # logger.warning is called with a printf-style format string + args; render it
    # the same way the real logger would so we can assert on the final message text.
    assert mock_warning.called, "Expected warning log when Tika XHTML returns 422"
    fmt, *args = mock_warning.call_args.args
    rendered = fmt % tuple(args)
    assert "Tika rejected XHTML" in rendered
    assert "Page tree root must be a dictionary" in rendered


# --- _sanitize_external_string ---


def test_sanitize_external_string_strips_ansi():
    s = "\x1b[31mred\x1b[0m text"
    assert _sanitize_external_string(s) == "red text"


def test_sanitize_external_string_strips_control_chars():
    # Bell (\x07), backspace (\x08), DEL (\x7f) — all control, all stripped.
    # Tab and newline are converted to spaces by the whitespace-collapse step.
    s = "before\x07\x08after\x7f"
    assert _sanitize_external_string(s) == "beforeafter"


def test_sanitize_external_string_collapses_whitespace():
    s = "line one\n\nline   two\t\ttabs"
    assert _sanitize_external_string(s) == "line one line two tabs"


def test_sanitize_external_string_caps_length():
    s = "x" * 1000
    out = _sanitize_external_string(s, max_chars=50)
    assert len(out) == 50


def test_sanitize_external_string_handles_empty():
    assert _sanitize_external_string("") == ""


# --- is_plain_text_source ---


def test_is_plain_text_source_by_mime():
    assert is_plain_text_source("text/plain", "") is True


def test_is_plain_text_source_new_extensions():
    for key in ("notes.rst", "data.json", "script.py", "subs.srt", "graph.canvas", "doc.markdown"):
        assert is_plain_text_source("", key) is True, key


def test_is_plain_text_source_legacy_plain_text():
    for key in ("readme.txt", "notes.md", "table.csv"):
        assert is_plain_text_source("", key) is True, key


def test_is_plain_text_source_tika_formats_excluded():
    for key in ("report.pdf", "memo.docx", "sheet.xlsx", "page.html", "book.epub"):
        assert is_plain_text_source("", key) is False, key
