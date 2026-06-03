"""Shared file-type classification: the extension allowlist and routing sets.

This module imports nothing from ``harbor_clerk`` so that both the API layer
(``api/routes/uploads.py``, ``api/routes/watch.py``) and the watcher
(``watcher/events.py``) can import it without creating a dependency chain.
It is the single source of truth — do not re-declare these sets elsewhere.
"""

import mimetypes
from pathlib import Path

# Extensions extracted as plain UTF-8 text (no Apache Tika).
PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
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
)

# Markdown-family extensions — these get the full Markdown treatment in Phase 2.
MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown"})

# Formats extracted via Apache Tika.
_TIKA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".rtf",
        ".odt",
        ".pages",
        ".xlsx",
        ".xls",
        ".ods",
        ".numbers",
        ".pptx",
        ".ppt",
        ".odp",
        ".key",
        ".epub",
        ".html",
        ".htm",
        ".eml",
    }
)

# Image formats (OCR-only, no text extraction).
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".tiff", ".tif"})

# The full set of accepted file extensions.
ALLOWED_EXTENSIONS: frozenset[str] = PLAIN_TEXT_EXTENSIONS | _TIKA_EXTENSIONS | _IMAGE_EXTENSIONS


# HC's curated mime types for extensions where Python's `mimetypes` either
# returns None or returns a mapping inconsistent with HC's intent (e.g. `.org`
# is overwhelmingly Emacs Org-mode in HC corpora, not legacy Lotus Organizer).
# These OVERRIDE mimetypes.guess_type so the value is deterministic across
# Python minor versions and across hosts. Keys must be lowercase, leading-dot.
_MIME_OVERRIDES: dict[str, str] = {
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".rst": "text/x-rst",
    ".org": "text/x-org",  # Emacs Org-mode, not Lotus Organizer
    ".adoc": "text/x-asciidoc",
    ".log": "text/plain",
    ".ipynb": "application/x-ipynb+json",
    ".canvas": "application/x-obsidian-canvas",
    ".pages": "application/x-iwork-pages",
    ".numbers": "application/x-iwork-numbers",
    ".key": "application/x-iwork-keynote",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    # Belt-and-suspenders: pin these so we never regress if a future Python
    # minor changes the mapping. Both are RFC-registered.
    ".markdown": "text/markdown",
    ".epub": "application/epub+zip",
}


def guess_mime_type(filename: str) -> str:
    """Return a best-guess MIME type from ``filename``'s extension.

    Precedence: HC's curated overrides → Python's ``mimetypes.guess_type`` →
    ``application/octet-stream`` (RFC 2046's "no information" sentinel —
    the right thing to send downstream including to Tika).

    Pure extension-based — never reads file contents.
    """
    ext = Path(filename).suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# Public surface of this module. ``ALLOWED_EXTENSIONS`` is the union, never
# referenced inside this file but consumed by uploads / watcher / extract;
# ``__all__`` makes that intent explicit and silences CodeQL's
# ``py/unused-global-variable`` query for the union sentinel.
__all__ = [
    "PLAIN_TEXT_EXTENSIONS",
    "MARKDOWN_EXTENSIONS",
    "ALLOWED_EXTENSIONS",
    "guess_mime_type",
    "is_excalidraw",
    "sniff_mime",
]


def is_excalidraw(path: str) -> bool:
    """True for Obsidian Excalidraw notes (``*.excalidraw.md``).

    These files carry a large compressed-JSON blob rather than prose and would
    pollute the search index if ingested as Markdown, so they are skipped at
    every ingest entry point.
    """
    return path.lower().endswith(".excalidraw.md")


def sniff_mime(data: bytes) -> str | None:
    """Best-effort content-based MIME detection from a file's leading bytes.

    Returns a canonical MIME string when the leading bytes carry a recognized
    magic-number signature, else ``None`` (unknown / text / not enough bytes).
    Pure-stdlib, no native dependency — covers the binary formats an office
    routinely mis-extensions (an Excel workbook saved as ``.pdf``, a scanned
    image renamed to ``.docx``, etc.). The extract stage uses this to correct
    a misrouted file before handing it to the wrong extractor.

    Container formats that magic bytes alone can't fully disambiguate
    (ZIP-based Office vs ODF vs EPUB; OLE2 .doc vs .xls vs .ppt) return the
    generic container type — the caller hands those to Tika, which does the
    fine-grained detection from the full byte stream.

    Image detection is deliberately limited to the formats the OCR stage can
    process (JPEG/PNG/TIFF — see ``IMAGE_MIMES`` in ``worker/stages/extract``).
    Don't add a signature here (e.g. GIF, BMP) without also adding it to the
    OCR dispatch, or the extract-stage corrector will route the file to a path
    that silently drops it.
    """
    if len(data) < 4:
        return None

    # Exact-prefix signatures, longest / most-specific first.
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:5] == b"{\\rtf":
        return "text/rtf"
    if data[:4] == b"PK\x03\x04":
        # ZIP container — modern Office (docx/xlsx/pptx), ODF, EPUB, etc.
        return "application/zip"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2 compound document — legacy Office (.doc/.xls/.ppt), .msg.
        return "application/x-ole-storage"

    return None
