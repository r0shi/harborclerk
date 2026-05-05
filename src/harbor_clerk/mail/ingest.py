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

import hashlib
import io
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.parser import EmailParseResult, sanitize_subject_for_filename
from harbor_clerk.models import Document, WatchedLabel
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.storage import get_storage

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


async def create_email_document(
    session: AsyncSession,
    *,
    parsed: EmailParseResult,
    eml_bytes: bytes,
    eml_sha256: bytes,
    label: WatchedLabel,
) -> Document:
    """Create the email Document, save the .eml to storage, return the row.

    Caller flushes/commits. Caller is responsible for also creating attachment
    Documents (Task 7) and updating the watched_message pointer.

    The Document's `created_at` is set to `email_date_sent` (per spec) so the
    Documents page sorts by send date, not ingest time.
    """
    doc_id = uuid.uuid4()
    safe_subject = sanitize_subject_for_filename(parsed.subject)
    storage_key = f"originals/{doc_id}/{safe_subject}.eml"

    storage = get_storage()
    storage.put_object(
        bucket="originals",
        key=storage_key,
        data=io.BytesIO(eml_bytes),
        length=len(eml_bytes),
        content_type="message/rfc822",
    )

    # Use the spec's send-date-as-created-at semantics. Fall back to now()
    # when the email had no Date header (rare).
    created_at = parsed.date_sent or datetime.now(UTC)

    doc = Document(
        doc_id=doc_id,
        title=parsed.subject,
        canonical_filename=f"{safe_subject}.eml",
        sha256=eml_sha256,
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        size_bytes=len(eml_bytes),
        original_object_key=storage_key,
        original_bucket="originals",
        email_message_id=parsed.message_id,
        email_thread_id=parsed.thread_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_to_addresses=parsed.to_addresses or None,
        email_cc_addresses=parsed.cc_addresses or None,
        email_date_sent=parsed.date_sent,
        email_label_path=label.label_path,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(doc)
    return doc


async def create_attachment_documents(
    session: AsyncSession,
    *,
    parsed: EmailParseResult,
    parent_doc: Document,
    label: WatchedLabel,
) -> list[Document]:
    """Create one Document per parsed attachment. Bytes saved to storage,
    linked back to the parent email via email_parent_doc_id.

    Caller flushes/commits.
    """
    storage = get_storage()
    docs: list[Document] = []
    for attachment in parsed.attachments:
        doc_id = uuid.uuid4()
        # Use the original filename (sanitized to be path-safe) for storage
        safe_filename = sanitize_subject_for_filename(attachment.filename)
        storage_key = f"originals/{doc_id}/{safe_filename}"

        storage.put_object(
            bucket="originals",
            key=storage_key,
            data=io.BytesIO(attachment.content),
            length=len(attachment.content),
            content_type=attachment.mime_type,
        )

        sha = hashlib.sha256(attachment.content).digest()

        doc = Document(
            doc_id=doc_id,
            title=attachment.filename,
            canonical_filename=safe_filename,
            sha256=sha,
            pipeline_status=PipelineStatus.queued,
            mime_type=attachment.mime_type,
            size_bytes=len(attachment.content),
            original_object_key=storage_key,
            original_bucket="originals",
            email_parent_doc_id=parent_doc.doc_id,
            email_message_id=parsed.message_id,
            email_label_path=label.label_path,
            email_date_sent=parsed.date_sent,
            created_at=parsed.date_sent or datetime.now(UTC),
            updated_at=parsed.date_sent or datetime.now(UTC),
        )
        session.add(doc)
        docs.append(doc)
    return docs
