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

The sidecar file itself is not indexed: the watcher (`watcher/events.py`,
`SkipReason.SIDECAR`) skips a `.json` that `load_sidecar` accepts and whose
stem matches a sibling the watcher would ingest, since a second document
stating the first one's facts is what a search would find first (#728), and
re-queues that sibling's extraction when the sidecar is created, changed or
deleted, so the attached metadata follows the file. A `.json` with no such
sibling, or one this module would decline (over the size cap, malformed,
not an object, empty), is an ordinary document. `load_sidecar` is the one
predicate both sides use, so a file is either attached or indexed, never
neither.

Returns None for docs without source_path (legacy uploads), without a
matching sidecar file, with malformed JSON, with an empty object, or
with a non-object top-level JSON value (list/string/etc).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# The sidecar is `<stem>.json` with this exact suffix, on both sides: what
# `with_suffix` produces here is what the watcher looks for. `INV-001.JSON` is
# a document on a case-sensitive filesystem, and is treated as one everywhere.
SIDECAR_SUFFIX = ".json"

# Above this the payload would bloat doc_metadata (and every search row that
# carries it); such a file is a document, not metadata.
MAX_SIDECAR_BYTES = 64_000


def sidecar_path_for(source_path: str) -> Path:
    """Where a document's sidecar would be: `<stem>.json` beside it."""
    return Path(source_path).with_suffix(SIDECAR_SUFFIX)


def load_sidecar(sidecar: Path, *, owner: str = "<unknown>", quiet: bool = False) -> dict | None:
    """The metadata `sidecar` carries, or None when it is not a usable sidecar.

    `owner` names the document (or path) in log lines. The extract stage
    logs every refusal at warning level, because a user who wrote the file
    expects it to be attached. The watcher asks the same question on every
    event and every scan, for files that may simply be JSON documents beside
    a same-stem neighbour (`data.json` beside `data.csv`), so it passes
    `quiet=True` and the refusals go to debug.
    """
    refused = log.debug if quiet else log.warning
    if not sidecar.is_file():
        return None
    try:
        size = sidecar.stat().st_size
    except OSError as exc:
        refused("sidecar stat failed for %s (%s): %s", owner, sidecar, exc)
        return None
    if size > MAX_SIDECAR_BYTES:
        refused(
            "sidecar for %s is %d bytes (>%d cap); ignoring to avoid bloating doc_metadata",
            owner,
            size,
            MAX_SIDECAR_BYTES,
        )
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError and UnicodeDecodeError alike.
        refused("sidecar load failed for %s (%s): %s", owner, sidecar, exc)
        return None

    if not isinstance(payload, dict):
        refused(
            "sidecar for %s is not a JSON object (got %s); ignoring",
            owner,
            type(payload).__name__,
        )
        return None

    return payload or None


class SidecarExtractor:
    """Loads <stem>.json next to the source file."""

    name = "sidecar"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        if not source_path:
            return None
        return load_sidecar(sidecar_path_for(source_path), owner=f"doc {getattr(doc, 'doc_id', '<unknown>')}")
