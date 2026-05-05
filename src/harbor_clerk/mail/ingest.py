"""Stage 3: turn watched_messages into Documents.

The sync engine (Stage 2) inserts a watched_message row with a placeholder
eml_sha256 each time it discovers a UID. Stage 3 picks up those rows and:
  1. Fetches the full .eml bytes via IMAP BODY.PEEK[].
  2. Computes the real SHA-256 of the bytes.
  3. Cross-label dedup: if any other watched_message already references a
     Document with this SHA, link to that existing Document.
  4. Otherwise: parse, save originals to storage, create email Document
     plus N attachment Documents, enqueue extract on each.
  5. Update the watched_message: real eml_sha256 + email_doc_id.

Documents are NOT deleted from this module — the lifecycle handler in
`document_lifecycle.py` does that on watched_message status transitions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from harbor_clerk.mail.imap_client import IMAPConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestSummary:
    """What an ingest invocation did."""

    fetched_count: int
    new_email_doc_count: int
    new_attachment_doc_count: int
    deduped_count: int  # rows that linked to an existing Document via SHA match


async def fetch_eml_bytes(conn: IMAPConnection, uid: int) -> bytes:
    """FETCH UID <uid> BODY.PEEK[] and return the raw RFC 5322 bytes.

    BODY.PEEK[] is BODY[] without setting the \\Seen flag — important for
    a read-only sync engine that shouldn't mutate the user's mailbox.
    """
    result, lines = await conn.client.uid("FETCH", str(uid), "BODY.PEEK[]")
    if result != "OK":
        raise RuntimeError(f"FETCH UID {uid} failed: {result}")
    return _extract_literal(lines)


def _extract_literal(fetch_lines: list[bytes]) -> bytes:
    """Pull the RFC 5322 message bytes out of a FETCH response.

    The structural line ends with `{<length>}` and the next N bytes are
    the message body. We collect the body bytes until we hit the
    closing `)` line.
    """
    body_chunks: list[bytes] = []
    expect_body = False
    for line in fetch_lines:
        if expect_body:
            if line == b")" or line.startswith(b")"):
                break
            body_chunks.append(line)
            continue
        # Structural line: `1 (UID 1 BODY[] {1234}` — after this, the message body lines follow
        if re.search(rb"\{\d+\}\s*$", line):
            expect_body = True
    # Reconstruct: aioimaplib splits the literal on \r\n; we need to rejoin
    return b"\r\n".join(body_chunks)
