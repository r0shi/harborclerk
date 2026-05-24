# src/harbor_clerk/ingest/metadata_extractors/__init__.py
"""Pluggable metadata extractors for HC's ingest pipeline.

Each extractor returns a dict of fields keyed under its `name` namespace
on the document's metadata JSONB column. The framework runs them in
EXTRACTORS order, merges results, and records per-source provenance
timestamps. A failing extractor logs a warning but does not abort the
others — ingestion stays resilient to one-off Tika hiccups or malformed
frontmatter.

Public surface:
  MetadataExtractor — @runtime_checkable Protocol
  EXTRACTORS        — production list, used by extract.py
  run_all(...)      — production entry point (uses EXTRACTORS)
  _run_extractors() — testable helper that takes an explicit extractor list
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class MetadataExtractor(Protocol):
    """A single extractor; one per metadata source.

    Implementations must declare `name` (the namespace key on the merged
    metadata dict) and `extract(*, doc, raw_bytes, source_path) -> dict | None`.
    Returning None signals "this extractor doesn't apply to this doc"
    (e.g. frontmatter extractor on a PDF) — the namespace is omitted
    entirely from the merged output.
    """

    name: str

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None: ...


def _run_extractors(
    extractors: list[MetadataExtractor],
    *,
    doc,
    raw_bytes: bytes,
    source_path: str | None,
) -> dict[str, Any]:
    """Run the given extractor list and merge results. See run_all() docstring."""
    out: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for ext in extractors:
        try:
            result = ext.extract(doc=doc, raw_bytes=raw_bytes, source_path=source_path)
        except Exception as exc:
            log.warning(
                "metadata extractor %s failed on doc %s: %s",
                ext.name,
                getattr(doc, "doc_id", "<unknown>"),
                exc,
            )
            continue
        if result:
            out[ext.name] = result
            provenance[ext.name] = datetime.now(UTC).isoformat()
    if out:
        out["_source_provenance"] = provenance
    return out


# Production extractor tuple — populated by individual extractors in
# later tasks. Keep at the bottom of the module so the imports below
# can reference symbols defined above.
from harbor_clerk.ingest.metadata_extractors.tika_metadata import TikaMetadataExtractor  # noqa: E402

EXTRACTORS: list[MetadataExtractor] = [TikaMetadataExtractor()]


def run_all(*, doc, raw_bytes: bytes, source_path: str | None) -> dict[str, Any]:
    """Entry point used by the extract stage. Runs EXTRACTORS in order,
    merges results, returns a namespaced dict suitable for assigning to
    Document.doc_metadata."""
    return _run_extractors(EXTRACTORS, doc=doc, raw_bytes=raw_bytes, source_path=source_path)
