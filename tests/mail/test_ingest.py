"""Tests for the email-ingest pipeline (watched_messages → Documents)."""

from datetime import UTC, datetime

import pytest

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.ingest import create_attachment_documents, create_email_document, fetch_eml_bytes
from harbor_clerk.mail.parser import AttachmentSpec, EmailParseResult
from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
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
