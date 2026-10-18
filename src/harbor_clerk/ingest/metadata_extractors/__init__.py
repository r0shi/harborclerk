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
import os
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import Select, select

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


# Production extractors — registered here in declaration order. Kept at the
# bottom of the module so the imports below can reference symbols defined above.
from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor  # noqa: E402
from harbor_clerk.ingest.metadata_extractors.sidecar import SidecarExtractor  # noqa: E402
from harbor_clerk.ingest.metadata_extractors.tika_metadata import TikaMetadataExtractor  # noqa: E402

EXTRACTORS: list[MetadataExtractor] = [
    TikaMetadataExtractor(),
    FrontmatterExtractor(),
    SidecarExtractor(),
]


def watched_paths_query(doc_id) -> Select:
    """``(folder path, relative path)`` of each active watched file, in an enabled
    folder, that maps to the document. Usable with a sync or an async session;
    feed the rows to ``verified_in_place_path``."""
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

    return (
        select(WatchedFolder.path, WatchedFile.relative_path)
        .join(WatchedFolder, WatchedFolder.folder_id == WatchedFile.folder_id)
        .where(
            WatchedFile.doc_id == doc_id,
            WatchedFile.status == WatchedFileStatus.active,
            WatchedFolder.enabled.is_(True),
        )
    )


def verified_in_place_path(rows, source_path: str | None) -> str | None:
    """The path a document is read in place from, or None.

    It is computed from the watched file (its folder's path, or that path's
    realpath, joined with its relative path), never taken from the document,
    and returned only when the document's ``source_path`` is exactly that path.
    A ``source_path`` set any other way is a label a client supplied, not a file
    this process may look beside. A relative path that is not already normal (a
    ``..`` or ``.`` segment, a doubled slash) matches nothing.
    """
    if not source_path:
        return None
    for folder_path, relative_path in rows:
        for root in dict.fromkeys((folder_path, os.path.realpath(folder_path))):
            watched = os.path.join(root, relative_path)
            if os.path.normpath(watched) == watched and watched == source_path:
                return watched
    return None


def run_all(*, doc, raw_bytes: bytes, in_place_path: str | None) -> dict[str, Any]:
    """Entry point used by the extract stage. Runs EXTRACTORS in order,
    merges results, returns a namespaced dict suitable for assigning to
    Document.doc_metadata.

    ``in_place_path`` is what ``verified_in_place_path`` returned for the document, the
    only path the extractors receive (as ``source_path``): None for an upload or
    a mail message, so nothing beside a client's label (a sidecar) is read.
    """
    return _run_extractors(EXTRACTORS, doc=doc, raw_bytes=raw_bytes, source_path=in_place_path)
