# src/harbor_clerk/ingest/metadata_extractors/frontmatter.py
"""FrontmatterExtractor — parses YAML frontmatter from markdown files.

Obsidian, MkDocs, Jekyll, Hugo all use the `---`-delimited YAML header
pattern at the top of markdown files. The extractor parses that header
and returns it as the metadata dict. For non-markdown files (PDFs, .eml,
.docx, etc.) it returns None.

Frontmatter values are normalized to JSON-serializable scalars: YAML
dates → ISO strings at the boundary (the doc_metadata column is JSONB,
which doesn't have a native date type — keeping dates as strings makes
filter values comparable without further parsing).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import frontmatter

log = logging.getLogger(__name__)

_MARKDOWN_EXTENSIONS = (".md", ".markdown")


class FrontmatterExtractor:
    """YAML frontmatter parser for markdown files."""

    name = "frontmatter"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        # Identify markdown via canonical_filename first, fall back to source_path
        filename = getattr(doc, "canonical_filename", None) or source_path or ""
        if not any(filename.lower().endswith(ext) for ext in _MARKDOWN_EXTENSIONS):
            return None

        try:
            parsed = frontmatter.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            log.warning(
                "frontmatter parse failed for doc %s: %s",
                getattr(doc, "doc_id", "<unknown>"),
                exc,
            )
            return None

        if not parsed.metadata:
            return None

        return _jsonify(parsed.metadata)


def _jsonify(obj: Any) -> Any:
    """Recursively convert YAML-parsed values to JSON-serializable equivalents.
    Mainly: date/datetime → ISO string."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj
