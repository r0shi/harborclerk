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

import asyncio
import hashlib
import io
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.parser import EmailParseResult, parse_eml, sanitize_subject_for_filename
from harbor_clerk.models import Document, WatchedLabel, WatchedMessage
from harbor_clerk.models.enums import JobStage, PipelineStatus
from harbor_clerk.storage import get_storage
from harbor_clerk.worker.pipeline import enqueue_stage

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
    bucket = get_settings().minio_bucket

    # storage.put_object is sync (blocking I/O). Wrap in run_in_executor so
    # large attachments don't stall the mail observer's shared event loop.
    # Same pattern as uploads.py:619 and the enqueue_stage call below.
    storage = get_storage()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: storage.put_object(
            bucket=bucket,
            key=storage_key,
            data=io.BytesIO(eml_bytes),
            length=len(eml_bytes),
            content_type="message/rfc822",
        ),
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
        original_bucket=bucket,
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
    bucket = get_settings().minio_bucket
    loop = asyncio.get_running_loop()
    docs: list[Document] = []
    for attachment in parsed.attachments:
        doc_id = uuid.uuid4()
        # Use the original filename (sanitized to be path-safe) for storage
        safe_filename = sanitize_subject_for_filename(attachment.filename)
        storage_key = f"originals/{doc_id}/{safe_filename}"

        # See create_email_document for why this is wrapped.
        await loop.run_in_executor(
            None,
            lambda key=storage_key, content=attachment.content, mime=attachment.mime_type: storage.put_object(
                bucket=bucket,
                key=key,
                data=io.BytesIO(content),
                length=len(content),
                content_type=mime,
            ),
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
            original_bucket=bucket,
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


async def ingest_pending_messages(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> IngestSummary:
    """For each watched_message in this label with email_doc_id=NULL:
    fetch the .eml, dedup by SHA across labels, create Documents (or link
    to existing), enqueue extract. Caller commits.
    """
    pending = (
        (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.email_doc_id.is_(None),
                    WatchedMessage.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )

    fetched_count = 0
    new_email_doc_count = 0
    new_attachment_doc_count = 0
    deduped_count = 0

    for msg in pending:
        try:
            eml_bytes = await fetch_eml_bytes(conn, msg.imap_uid)
        except Exception as exc:
            logger.warning(
                "fetch_eml_bytes failed for label=%s uid=%s: %s",
                label.label_id,
                msg.imap_uid,
                exc,
            )
            continue
        fetched_count += 1
        real_sha = hashlib.sha256(eml_bytes).digest()
        # IMPORTANT: assigning real_sha to this row's eml_sha256 BEFORE the
        # cross-label dedup query below is safe because:
        #   1. SQLAlchemy's autoflush will write the new value before the SELECT.
        #   2. The SELECT filters on `email_doc_id.is_not(None)`, and this row's
        #      email_doc_id is still NULL — so it cannot match itself.
        # If a future refactor removes the email_doc_id IS NOT NULL clause, move
        # the eml_sha256 assignment to AFTER the dedup query (and after we either
        # linked or created a Document) to prevent self-matching.
        msg.eml_sha256 = real_sha

        # Cross-label dedup: any other watched_message already mapped to a Document with this SHA?
        existing = (
            (
                await session.execute(
                    select(WatchedMessage).where(
                        WatchedMessage.eml_sha256 == real_sha,
                        WatchedMessage.email_doc_id.is_not(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            msg.email_doc_id = existing.email_doc_id
            deduped_count += 1
            continue

        try:
            parsed = parse_eml(eml_bytes)
            email_doc = await create_email_document(
                session,
                parsed=parsed,
                eml_bytes=eml_bytes,
                eml_sha256=real_sha,
                label=label,
            )
            await session.flush()

            attachment_docs = await create_attachment_documents(
                session,
                parsed=parsed,
                parent_doc=email_doc,
                label=label,
            )
            await session.flush()

            msg.email_doc_id = email_doc.doc_id

            # enqueue_stage is sync (uses psycopg2). Run in the default thread executor
            # so the event loop stays responsive — the mail observer's per-label tasks
            # all share one event loop, and a large ingest batch could otherwise stall
            # IMAP IDLE handling for other labels.
            loop = asyncio.get_running_loop()
            for d in [email_doc, *attachment_docs]:
                await loop.run_in_executor(None, enqueue_stage, d.doc_id, JobStage.extract)

            new_email_doc_count += 1
            new_attachment_doc_count += len(attachment_docs)
        except Exception as exc:
            logger.warning(
                "ingest failed for label=%s uid=%s: %s — leaving for next tick",
                label.label_id,
                msg.imap_uid,
                exc,
            )
            # Roll back this savepoint so other messages in the batch still commit.
            # Without per-message savepoints we'd need session.rollback() which
            # would discard the eml_sha256 update too. Instead: reset email_doc_id
            # and continue. Next tick will re-pick this message via email_doc_id IS NULL.
            msg.email_doc_id = None
            continue

    return IngestSummary(
        fetched_count=fetched_count,
        new_email_doc_count=new_email_doc_count,
        new_attachment_doc_count=new_attachment_doc_count,
        deduped_count=deduped_count,
    )
