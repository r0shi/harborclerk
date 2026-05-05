"""Verify the email-metadata columns on Document map correctly."""

from datetime import UTC, datetime

from sqlalchemy import select

from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus

# Minimal required fields for a Document row (sha256 + pipeline_status are NOT NULL).
# Mirrors _DOC_DEFAULTS in tests/test_api_documents.py.
_DOC_DEFAULTS = {
    "sha256": b"\x00" * 32,
    "pipeline_status": PipelineStatus.ready,
}


async def test_email_metadata_round_trip(db_session):
    doc = Document(
        title="Test email",
        canonical_filename="test.eml",
        mime_type="message/rfc822",
        email_message_id="<abc@example.com>",
        email_thread_id="thread-1",
        email_from_address="alice@example.com",
        email_from_name="Alice",
        email_to_addresses=["alex@example.com", "bob@example.com"],
        email_cc_addresses=["legal@example.com"],
        email_date_sent=datetime(2026, 4, 30, 14, 23, tzinfo=UTC),
        email_label_path="Clerk",
        **_DOC_DEFAULTS,
    )
    db_session.add(doc)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(Document).where(Document.email_message_id == "<abc@example.com>"))
    ).scalar_one()
    assert fetched.email_from_address == "alice@example.com"
    assert fetched.email_to_addresses == ["alex@example.com", "bob@example.com"]
    assert fetched.email_cc_addresses == ["legal@example.com"]
    assert fetched.email_label_path == "Clerk"
    assert fetched.email_date_sent == datetime(2026, 4, 30, 14, 23, tzinfo=UTC)


async def test_email_columns_optional_for_non_email_docs(db_session):
    """Watched-folder and uploaded docs should not need to set email_* fields."""
    doc = Document(
        title="A regular PDF",
        canonical_filename="report.pdf",
        mime_type="application/pdf",
        **_DOC_DEFAULTS,
    )
    db_session.add(doc)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(Document).where(Document.canonical_filename == "report.pdf"))
    ).scalar_one()
    assert fetched.email_message_id is None
    assert fetched.email_to_addresses is None


async def test_email_parent_doc_id_self_reference(db_session):
    """Attachment Documents link to their parent email Document via email_parent_doc_id."""
    parent = Document(
        title="Parent email",
        canonical_filename="parent.eml",
        mime_type="message/rfc822",
        email_message_id="<parent@example.com>",
        **_DOC_DEFAULTS,
    )
    db_session.add(parent)
    await db_session.flush()

    attachment = Document(
        title="contract.pdf",
        canonical_filename="contract.pdf",
        mime_type="application/pdf",
        email_parent_doc_id=parent.doc_id,
        email_message_id="<parent@example.com>",
        **_DOC_DEFAULTS,
    )
    db_session.add(attachment)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(Document).where(Document.canonical_filename == "contract.pdf"))
    ).scalar_one()
    assert fetched.email_parent_doc_id == parent.doc_id
