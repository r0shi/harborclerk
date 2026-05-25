"""Effective-date discovery for documents.

Walks a priority chain to return the canonical date for a document:
  1. metadata.tika.created_at  (Tika-extracted email-send / file-creation date)
  2. metadata.frontmatter.date (markdown frontmatter)
  3. metadata.sidecar.date     (JSON sidecar)
  4. documents.created_at      (ingest time — last resort)

Used by kb_documents_by_date for sorting + result annotation. Standalone
module so future features (document list views, exports, filters) can
share one place for "the canonical date for this doc."
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Priority chain: (label, getter). First non-null parseable value wins.
_PRIORITY: list[tuple[str, Callable[[Any], Any]]] = [
    ("tika.created_at", lambda d: (d.doc_metadata or {}).get("tika", {}).get("created_at")),
    (
        "frontmatter.date",
        lambda d: (d.doc_metadata or {}).get("frontmatter", {}).get("date"),
    ),
    ("sidecar.date", lambda d: (d.doc_metadata or {}).get("sidecar", {}).get("date")),
]


def effective_date(doc) -> tuple[datetime | None, str]:
    """Return (effective_date, source_label) for a document.

    Walks the priority chain and returns the first non-null value that
    parses as a datetime. Falls back to `documents.created_at` (ingest
    time) if no metadata source provides a valid date. Returns
    `(None, "none")` only if every source is missing AND ingest is also
    null (unreachable in practice — documents always have created_at).

    source_label ∈ {"tika.created_at", "frontmatter.date", "sidecar.date",
                    "ingest", "none"}.
    """
    for label, getter in _PRIORITY:
        raw = getter(doc)
        parsed = _parse(raw)
        if parsed is not None:
            return parsed, label
    if getattr(doc, "created_at", None) is not None:
        return _ensure_utc(doc.created_at), "ingest"
    return None, "none"


def _parse(raw: Any) -> datetime | None:
    """Parse a value into a UTC-aware datetime. Returns None on failure."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalise "Z" suffix to "+00:00" since fromisoformat() only
    # accepts the latter on Python 3.10. Both forms are valid ISO 8601.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _ensure_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _ensure_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass aware datetimes through."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
