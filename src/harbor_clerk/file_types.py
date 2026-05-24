"""Shared file-type classification: the extension allowlist and routing sets.

This module imports nothing from ``harbor_clerk`` so that both the API layer
(``api/routes/uploads.py``, ``api/routes/watch.py``) and the watcher
(``watcher/events.py``) can import it without creating a dependency chain.
It is the single source of truth — do not re-declare these sets elsewhere.
"""

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


# Public surface of this module. ``ALLOWED_EXTENSIONS`` is the union, never
# referenced inside this file but consumed by uploads / watcher / extract;
# ``__all__`` makes that intent explicit and silences CodeQL's
# ``py/unused-global-variable`` query for the union sentinel.
__all__ = ["PLAIN_TEXT_EXTENSIONS", "MARKDOWN_EXTENSIONS", "ALLOWED_EXTENSIONS", "is_excalidraw"]


def is_excalidraw(path: str) -> bool:
    """True for Obsidian Excalidraw notes (``*.excalidraw.md``).

    These files carry a large compressed-JSON blob rather than prose and would
    pollute the search index if ingested as Markdown, so they are skipped at
    every ingest entry point.
    """
    return path.lower().endswith(".excalidraw.md")
