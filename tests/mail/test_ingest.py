"""Tests for the email-ingest pipeline (watched_messages → Documents)."""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.ingest import (
    create_attachment_documents,
    create_email_document,
    fetch_eml_bytes,
    ingest_pending_messages,
)
from harbor_clerk.mail.parser import AttachmentSpec, EmailParseResult
from harbor_clerk.models import Document, IngestionJob, WatchedMessage
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.mail.fixtures.build_eml import build_email_with_attachments


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_fetch_eml_bytes_returns_raw_message(mock_aioimap, watched_label):
    eml = b"From: a@example.com\r\nSubject: t\r\n\r\nBody"
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    fetched = await fetch_eml_bytes(conn, uid=1)
    await conn.logout()

    assert fetched == eml


async def test_create_email_document_persists_metadata(db_session, watched_label):
    parsed = EmailParseResult(
        message_id="<email1@example.com>",
        subject="Test email",
        from_address="alice@example.com",
        from_name="Alice",
        to_addresses=["bob@example.com"],
        cc_addresses=[],
        date_sent=datetime(2026, 4, 30, 14, 23, tzinfo=UTC),
        thread_id="thread-1",
        body_text="Body content",
    )
    eml_bytes = b"From: alice\r\nSubject: Test email\r\n\r\nBody content"
    eml_sha256 = b"\xab" * 32

    doc = await create_email_document(
        db_session,
        parsed=parsed,
        eml_bytes=eml_bytes,
        eml_sha256=eml_sha256,
        label=watched_label,
    )
    await db_session.flush()

    assert doc.title == "Test email"
    assert doc.email_message_id == "<email1@example.com>"
    assert doc.email_from_address == "alice@example.com"
    assert doc.email_from_name == "Alice"
    assert doc.email_to_addresses == ["bob@example.com"]
    assert doc.email_thread_id == "thread-1"
    assert doc.email_label_path == watched_label.label_path
    assert doc.email_date_sent == datetime(2026, 4, 30, 14, 23, tzinfo=UTC)
    assert doc.email_subject == "Test email"
    # created_at should equal date_sent (per spec — sort by send date)
    assert doc.created_at == datetime(2026, 4, 30, 14, 23, tzinfo=UTC)
    assert doc.mime_type == "message/rfc822"
    assert doc.sha256 == eml_sha256
    # original_object_key set to a path under originals/<doc_id>/...
    assert doc.original_object_key is not None
    assert str(doc.doc_id) in doc.original_object_key
    assert doc.original_object_key.endswith(".eml")


async def test_create_attachment_documents_links_to_parent(db_session, watched_label):
    # Set up parent email Document
    parent = Document(
        title="Parent email",
        canonical_filename="parent.eml",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        email_message_id="<parent@example.com>",
        email_label_path=watched_label.label_path,
    )
    db_session.add(parent)
    await db_session.flush()

    attachments = [
        AttachmentSpec(filename="contract.pdf", mime_type="application/pdf", content=b"%PDF-1.4 fake"),
        AttachmentSpec(
            filename="addendum.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"PK fake docx",
        ),
    ]
    parsed = EmailParseResult(
        message_id="<parent@example.com>",
        subject="Parent email",
        from_address="alice@example.com",
        from_name="Alice",
        date_sent=datetime(2026, 4, 30, tzinfo=UTC),
        attachments=attachments,
    )

    docs = await create_attachment_documents(
        db_session,
        parsed=parsed,
        parent_doc=parent,
        label=watched_label,
    )
    await db_session.flush()

    assert len(docs) == 2
    assert docs[0].title == "contract.pdf"
    assert docs[0].email_parent_doc_id == parent.doc_id
    assert docs[0].email_message_id == "<parent@example.com>"
    assert docs[0].mime_type == "application/pdf"
    assert docs[0].original_object_key.endswith("contract.pdf")
    # created_at inherits from parent's send date so attachments sort with their email
    assert docs[0].created_at == datetime(2026, 4, 30, tzinfo=UTC)

    assert docs[1].title == "addendum.docx"
    assert docs[1].email_parent_doc_id == parent.doc_id


async def test_ingest_creates_email_and_attachment_docs(db_session, watched_label, mock_aioimap, monkeypatch):
    from harbor_clerk.mail.ingest import ingest_pending_messages
    from harbor_clerk.models import WatchedMessage

    # Pre-populate a watched_message row (simulating Stage 2 sync output)
    eml = build_email_with_attachments(
        message_id="<full@example.com>",
        subject="Full ingest test",
        body_text="Body.",
        attachments=[("contract.pdf", "application/pdf", b"%PDF-1.4 fake")],
    )
    placeholder_sha = hashlib.sha256(b"placeholder").digest()
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<full@example.com>",
        imap_uid=42,
        eml_sha256=placeholder_sha,
        status="active",
        email_doc_id=None,  # not yet ingested
    )
    db_session.add(msg)
    await db_session.flush()

    # Mock IMAP FETCH to return the .eml bytes
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"42 (UID 42 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 1
    assert summary.new_email_doc_count == 1
    assert summary.new_attachment_doc_count == 1
    assert summary.deduped_count == 0

    # watched_message now has real SHA + email_doc_id pointer
    await db_session.refresh(msg)
    assert msg.eml_sha256 != placeholder_sha
    assert msg.eml_sha256 == hashlib.sha256(eml).digest()
    assert msg.email_doc_id is not None

    # Email Document exists
    email_doc = (await db_session.execute(select(Document).where(Document.doc_id == msg.email_doc_id))).scalar_one()
    assert email_doc.title == "Full ingest test"
    assert email_doc.mime_type == "message/rfc822"

    # Attachment Document exists, linked to email
    attachments = (
        (await db_session.execute(select(Document).where(Document.email_parent_doc_id == msg.email_doc_id)))
        .scalars()
        .all()
    )
    assert len(attachments) == 1
    assert attachments[0].title == "contract.pdf"


async def test_ingest_dedupes_across_labels_via_sha(db_session, mail_account, mock_aioimap, monkeypatch):
    """Same email already ingested via another label → reuse the existing
    email_doc_id without creating a new Document."""
    from harbor_clerk.mail.ingest import ingest_pending_messages
    from harbor_clerk.models import WatchedLabel, WatchedMessage

    label_a = WatchedLabel(account_id=mail_account.account_id, label_path="LabelA", display_name="LabelA")
    label_b = WatchedLabel(account_id=mail_account.account_id, label_path="LabelB", display_name="LabelB")
    db_session.add_all([label_a, label_b])
    await db_session.flush()

    eml = build_email_with_attachments(
        message_id="<dup@example.com>",
        subject="Dup",
        body_text="x",
    )
    real_sha = hashlib.sha256(eml).digest()

    # Pre-populate label_a with an already-ingested watched_message
    parent_doc = Document(
        title="Dup",
        canonical_filename="Dup.eml",
        sha256=real_sha,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id="<dup@example.com>",
        original_object_key="originals/existing/dup.eml",
        original_bucket="originals",
    )
    db_session.add(parent_doc)
    await db_session.flush()
    msg_a = WatchedMessage(
        label_id=label_a.label_id,
        message_id="<dup@example.com>",
        imap_uid=1,
        eml_sha256=real_sha,
        status="active",
        email_doc_id=parent_doc.doc_id,
    )
    db_session.add(msg_a)

    # Now label_b discovers the same message (placeholder SHA)
    placeholder_sha = hashlib.sha256(b"placeholder-b").digest()
    msg_b = WatchedMessage(
        label_id=label_b.label_id,
        message_id="<dup@example.com>",
        imap_uid=99,
        eml_sha256=placeholder_sha,
        status="active",
        email_doc_id=None,
    )
    db_session.add(msg_b)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"99 (UID 99 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, label_b)
    await conn.logout()
    await db_session.commit()

    assert summary.deduped_count == 1
    assert summary.new_email_doc_count == 0
    assert summary.new_attachment_doc_count == 0

    # msg_b should now point at the same Document as msg_a
    await db_session.refresh(msg_b)
    assert msg_b.email_doc_id == parent_doc.doc_id
    assert msg_b.eml_sha256 == real_sha


async def test_ingest_rolls_back_email_doc_when_attachment_creation_fails(
    db_session, watched_label, mock_aioimap, monkeypatch
):
    """Regression: a mid-batch failure in create_attachment_documents must
    NOT leave a permanent orphan email Document.

    Pre-fix flow: session.flush() wrote the email Document; then
    create_attachment_documents raised (e.g. storage failure); the except
    set msg.email_doc_id=None but the email Document row stayed in the
    outer transaction. mail_observer.on_tick's outer commit persisted it.
    Subsequent ticks couldn't dedup against it (cross-label dedup filters
    on email_doc_id IS NOT NULL), so they re-created the same Document
    on retry, accumulating one ghost per failure.

    Fix: per-message session.begin_nested() savepoint scopes rollback to
    this iteration. When create_attachment_documents raises, both the
    email Document and any partial attachments roll back together.
    """

    # Patch create_attachment_documents to always raise — simulates a
    # storage backend failure after the email .eml was successfully put.
    async def boom(*args, **kwargs):
        raise RuntimeError("simulated storage failure mid-batch")

    monkeypatch.setattr("harbor_clerk.mail.ingest.create_attachment_documents", boom)

    eml = build_email_with_attachments(
        message_id="<rollback@example.com>",
        subject="Rollback test",
        body_text="Body.",
        attachments=[("doomed.pdf", "application/pdf", b"%PDF-1.4 fake")],
    )
    placeholder_sha = hashlib.sha256(b"placeholder").digest()
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<rollback@example.com>",
        imap_uid=99,
        eml_sha256=placeholder_sha,
        status="active",
        email_doc_id=None,
    )
    db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"99 (UID 99 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    # No Documents survived for this message.
    assert summary.new_email_doc_count == 0
    assert summary.new_attachment_doc_count == 0
    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 0, f"orphan Document(s) survived rollback: {[d.title for d in docs]}"

    # msg.email_doc_id is still None — next tick will re-pick it via the
    # `email_doc_id IS NULL` SELECT in ingest_pending_messages.
    await db_session.refresh(msg)
    assert msg.email_doc_id is None
    # msg.eml_sha256 was updated OUTSIDE the savepoint, so the real SHA
    # persists across the failed iteration. Next tick reuses it.
    assert msg.eml_sha256 == hashlib.sha256(eml).digest()


async def test_ingest_enqueues_extract_for_each_new_doc(db_session, watched_label, mock_aioimap, monkeypatch):
    """ingest_pending_messages should queue extract for the email document and each attachment document."""

    eml = build_email_with_attachments(
        message_id="<enq@example.com>",
        subject="enqueue test",
        attachments=[
            ("a.pdf", "application/pdf", b"%PDF a"),
            ("b.pdf", "application/pdf", b"%PDF b"),
        ],
    )
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<enq@example.com>",
        imap_uid=7,
        eml_sha256=hashlib.sha256(b"placeholder").digest(),
        status="active",
        email_doc_id=None,
    )
    db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"7 (UID 7 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, watched_label)
    await conn.logout()

    assert summary.new_email_doc_count == 1
    assert summary.new_attachment_doc_count == 2

    jobs = (
        (
            await db_session.execute(
                select(IngestionJob).where(
                    IngestionJob.stage == JobStage.extract,
                    IngestionJob.status == JobStatus.queued,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 3  # email + 2 attachments
    assert {job.pipeline_seq for job in jobs} == {0}
