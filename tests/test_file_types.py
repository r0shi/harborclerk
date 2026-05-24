"""Unit tests for the shared file-type classification module."""

from harbor_clerk.file_types import (
    ALLOWED_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    PLAIN_TEXT_EXTENSIONS,
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
