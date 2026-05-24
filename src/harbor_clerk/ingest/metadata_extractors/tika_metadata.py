# src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py
"""TikaMetadataExtractor — captures Tika's metadata dict.

Tika's /meta endpoint returns a JSON dict with potentially 50-200 fields,
most of which are framework noise (X-TIKA-Parsed-By, X-TIKA-Content-Length,
parser-specific keys). The whitelist + alias map normalizes the wildly
varying Tika field names to readable filter keys; unknown fields are dropped.
No raw passthrough — keeps the metadata blob bounded and the filter surface
clean.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from harbor_clerk.config import get_settings

log = logging.getLogger(__name__)

# Tika field name → readable filter key. When two Tika fields map to the
# same target, the LAST one wins (intentional — Tika sometimes emits the
# same concept under multiple keys, and the most reliable one is listed
# last). Test pins the duplicate set so a future refactor surfaces changes.
TIKA_FIELD_ALIASES: dict[str, str] = {
    # Dublin Core
    "dc:creator": "author",
    "dc:title": "title",
    "dc:subject": "subject",
    "dc:description": "description",
    "dc:language": "language",
    "dcterms:created": "created_at",
    "dcterms:modified": "modified_at",
    "meta:keyword": "keywords",
    # Pagination / structure (last writer wins → Page-Count beats xmpTPg:NPages)
    "xmpTPg:NPages": "page_count",
    "Page-Count": "page_count",
    # MIME / encoding
    "Content-Type": "content_type",
    "Content-Encoding": "encoding",
    # Email headers (Tika's email parser populates these for .eml files)
    "Message-From": "email_from",
    "Message-To": "email_to",
    "Message-Cc": "email_cc",
    "Message-Subject": "email_subject",
}


class TikaMetadataExtractor:
    """Calls Tika's /meta endpoint, whitelists fields, drops noise."""

    name = "tika"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        settings = get_settings()
        if not settings.tika_url:
            return None
        try:
            with httpx.Client(timeout=30) as client:
                headers = {"Accept": "application/json"}
                if getattr(doc, "mime_type", None):
                    headers["Content-Type"] = doc.mime_type
                resp = client.put(
                    f"{settings.tika_url}/meta",
                    content=raw_bytes,
                    headers=headers,
                )
                if resp.status_code != 200:
                    log.warning(
                        "tika /meta returned HTTP %s for doc %s",
                        resp.status_code,
                        getattr(doc, "doc_id", "<unknown>"),
                    )
                    return None
                raw: dict[str, Any] = resp.json()
                if not isinstance(raw, dict):
                    log.warning(
                        "tika /meta returned non-dict for doc %s: %s",
                        getattr(doc, "doc_id", "<unknown>"),
                        type(raw).__name__,
                    )
                    return None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("tika /meta failed for doc %s: %s", getattr(doc, "doc_id", "<unknown>"), exc)
            return None

        # Whitelist + alias
        out: dict[str, Any] = {}
        for tika_key, target_key in TIKA_FIELD_ALIASES.items():
            if tika_key in raw:
                out[target_key] = raw[tika_key]

        return out or None
