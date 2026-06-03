"""Unit tests for the shared file-type classification module."""

import pytest

from harbor_clerk.file_types import (
    ALLOWED_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    PLAIN_TEXT_EXTENSIONS,
    guess_mime_type,
    is_excalidraw,
    sniff_mime,
)


def test_legacy_extensions_still_allowed():
    """Every extension from the pre-existing allowlist must remain accepted."""
    legacy = {
        ".pdf",
        ".docx",
        ".doc",
        ".rtf",
        ".txt",
        ".md",
        ".odt",
        ".pages",
        ".xlsx",
        ".xls",
        ".ods",
        ".numbers",
        ".csv",
        ".pptx",
        ".ppt",
        ".odp",
        ".key",
        ".jpg",
        ".jpeg",
        ".png",
        ".tiff",
        ".tif",
        ".epub",
        ".html",
        ".htm",
        ".eml",
    }
    assert legacy <= ALLOWED_EXTENSIONS


def test_new_extensions_added():
    """The broadened text formats are now accepted."""
    new = {
        ".markdown",
        ".tsv",
        ".srt",
        ".vtt",
        ".rst",
        ".org",
        ".adoc",
        ".tex",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".log",
        ".ipynb",
        ".canvas",
    }
    assert new <= ALLOWED_EXTENSIONS


def test_plain_text_is_subset_of_allowed():
    assert PLAIN_TEXT_EXTENSIONS <= ALLOWED_EXTENSIONS


def test_markdown_set_is_exact():
    assert {".md", ".markdown"} == MARKDOWN_EXTENSIONS
    assert MARKDOWN_EXTENSIONS <= PLAIN_TEXT_EXTENSIONS


def test_new_text_formats_route_to_plain_text():
    for ext in (".rst", ".py", ".json", ".srt", ".canvas", ".markdown"):
        assert ext in PLAIN_TEXT_EXTENSIONS


def test_tika_formats_not_plain_text():
    """Office / PDF / image formats must NOT be on the plain-text path."""
    for ext in (".pdf", ".docx", ".xlsx", ".html", ".epub", ".png"):
        assert ext not in PLAIN_TEXT_EXTENSIONS


def test_is_excalidraw_true():
    assert is_excalidraw("Diagram.excalidraw.md") is True
    assert is_excalidraw("vault/sub/Sketch.EXCALIDRAW.MD") is True


def test_is_excalidraw_false():
    assert is_excalidraw("notes.md") is False
    assert is_excalidraw("report.pdf") is False
    assert is_excalidraw("my.excalidraw.txt") is False


def test_uploads_route_uses_shared_allowlist():
    """uploads.py must reference the shared set, not re-declare its own copy."""
    from harbor_clerk.api.routes import uploads

    assert uploads.ALLOWED_EXTENSIONS is ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# guess_mime_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        # Tika formats — Python's mimetypes covers these on all supported versions.
        ("report.pdf", "application/pdf"),
        ("brief.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("legacy.doc", "application/msword"),
        ("budget.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("page.html", "text/html"),
        ("mail.eml", "message/rfc822"),
        ("book.epub", "application/epub+zip"),
        # Plain text family.
        ("notes.txt", "text/plain"),
        ("readme.md", "text/markdown"),
        ("data.csv", "text/csv"),
        ("data.json", "application/json"),
        # Image formats route to the right image/ subtypes.
        ("scan.jpg", "image/jpeg"),
        ("logo.png", "image/png"),
        ("fax.tiff", "image/tiff"),
        # HC-specific extensions Python's registry may not know — covered by
        # the _MIME_FALLBACKS table so we get something better than octet-stream.
        ("captions.srt", "application/x-subrip"),
        ("notes.org", "text/x-org"),
        ("manual.adoc", "text/x-asciidoc"),
        ("compose.yaml", "application/yaml"),
        ("ci.yml", "application/yaml"),
        ("notebook.ipynb", "application/x-ipynb+json"),
        ("graph.canvas", "application/x-obsidian-canvas"),
        ("memo.pages", "application/x-iwork-pages"),
        ("model.numbers", "application/x-iwork-numbers"),
        ("pitch.key", "application/x-iwork-keynote"),
    ],
)
def test_guess_mime_type_known_extensions(filename, expected):
    assert guess_mime_type(filename) == expected


def test_guess_mime_type_is_extension_based_not_case_sensitive():
    """mimetypes.guess_type lowercases the extension internally; verify."""
    assert guess_mime_type("REPORT.PDF") == "application/pdf"
    assert guess_mime_type("Scan.TIFF") == "image/tiff"


def test_guess_mime_type_unknown_falls_back_to_octet_stream():
    """RFC 2046 'no information' sentinel — what Tika expects when given bytes blindly."""
    assert guess_mime_type("opaque.zzzzz") == "application/octet-stream"
    assert guess_mime_type("noextension") == "application/octet-stream"


# ---------------------------------------------------------------------------
# sniff_mime — content-based detection for misnamed files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"%PDF-1.7\n...", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
        (b"II*\x00\x08\x00", "image/tiff"),
        (b"MM\x00*\x00\x00", "image/tiff"),
        (b"GIF89a....", "image/gif"),
        (b"GIF87a....", "image/gif"),
        (b"{\\rtf1\\ansi", "text/rtf"),
        (b"PK\x03\x04\x14\x00", "application/zip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    ],
)
def test_sniff_mime_recognizes_signatures(data, expected):
    assert sniff_mime(data) == expected


def test_sniff_mime_returns_none_for_plain_text():
    assert sniff_mime(b"Dear Bob,\n\nThis is a plain text email.") is None


def test_sniff_mime_returns_none_for_too_short():
    assert sniff_mime(b"") is None
    assert sniff_mime(b"PK") is None  # 2 bytes — below the 4-byte floor


def test_sniff_mime_office_as_pdf_is_detected_as_zip():
    """The core misnamed-file case: an .xlsx workbook saved with a .pdf
    extension declares application/pdf but its bytes are a ZIP container.
    sniff_mime sees the ZIP magic, letting the extract stage reroute to Tika
    instead of the PDF parser (which would 422)."""
    xlsx_bytes = b"PK\x03\x04" + b"\x14\x00\x06\x00" + b"...[Content_Types].xml..."
    assert sniff_mime(xlsx_bytes) == "application/zip"


def test_sniff_mime_legacy_excel_is_ole():
    """A legacy .xls (OLE2 compound) renamed to .pdf is detected as OLE."""
    xls_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8
    assert sniff_mime(xls_bytes) == "application/x-ole-storage"
