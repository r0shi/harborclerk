# src/harbor_clerk/ingest/metadata_extractors/sidecar.py
"""SidecarExtractor — loads <stem>.json next to source_path.

For docs ingested via watched folders (synthetic test corpus, real-world
power users with curated metadata files), look for a JSON file with the
same stem as the source file and load it as the metadata dict.

Example: a watched folder containing
  invoices/2024-Q3/INV-001.pdf
  invoices/2024-Q3/INV-001.json
would surface the INV-001.json contents under the 'sidecar' namespace on
the Document for INV-001.pdf.

Returns None for docs without source_path (legacy uploads), without a
matching sidecar file, with malformed JSON, with an empty object, or
with a non-object top-level JSON value (list/string/etc).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SidecarExtractor:
    """Loads <stem>.json next to the source file."""

    name = "sidecar"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        if not source_path:
            return None
        src = Path(source_path)
        sidecar = src.with_suffix(".json")
        if not sidecar.is_file():
            return None
        try:
            size = sidecar.stat().st_size
        except OSError as exc:
            log.warning(
                "sidecar stat failed for doc %s (%s): %s",
                getattr(doc, "doc_id", "<unknown>"),
                sidecar,
                exc,
            )
            return None
        if size > 64_000:
            log.warning(
                "sidecar for doc %s is %d bytes (>64 KB cap); ignoring to avoid bloating doc_metadata",
                getattr(doc, "doc_id", "<unknown>"),
                size,
            )
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "sidecar load failed for doc %s (%s): %s",
                getattr(doc, "doc_id", "<unknown>"),
                sidecar,
                exc,
            )
            return None

        if not isinstance(payload, dict):
            log.warning(
                "sidecar for doc %s is not a JSON object (got %s); ignoring",
                getattr(doc, "doc_id", "<unknown>"),
                type(payload).__name__,
            )
            return None

        return payload or None
