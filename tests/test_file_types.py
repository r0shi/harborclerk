"""Unit tests for the shared file-type classification module."""

import pytest

from harbor_clerk.file_types import (
    ALLOWED_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    PLAIN_TEXT_EXTENSIONS,
    guess_mime_type,
    is_excalidraw,
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
