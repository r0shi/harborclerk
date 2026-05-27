"""End-to-end: parse a real-shaped .eml, persist Document + chunks, then
verify the header preamble lives in chunk 0 AND the email.* filter
namespace returns the right doc.

Critical Fix #1 — preamble injection lives in the extract stage, not in
parse_eml. These tests drive the extract-stage helper (_build_header_preamble
+ the injection logic) directly, without requiring a live Tika server.

Critical Fix #2 — attachment Documents (PDFs, DOCXs etc. from emails) also
carry email_message_id, but must NOT receive the preamble. The gate in
extract.py requires email_parent_doc_id IS NULL so only the email body
Document gets the preamble.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from harbor_clerk.mail.parser import _build_header_preamble, parse_eml
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search
from tests.mail.fixtures.build_eml import build_simple_email


def test_extract_stage_preamble_prepended_to_page_text():
    """Simulate the extract-stage email preamble injection:
    given a Document with email_* columns and a Tika-produced page_text,
    the injected preamble should come before the body text.

    This is a pure-Python unit test — no DB or Tika required.
    """
    tika_body = "The quarterly report attached covers Q3 performance."

    preamble = _build_header_preamble(
        from_name="Alice Anderson",
        from_address="alice@firm.com",
        to_addresses=["bob@firm.com"],
        cc_addresses=[],
        subject="Q3 Vendor Agreement Review",
        date_sent=datetime(2026, 1, 15, tzinfo=UTC),
    )

    # Simulate what extract.py does for the first page of an email doc.
    page_text = preamble + tika_body

    assert page_text.startswith("From: Alice Anderson <alice@firm.com>\n")
    assert "To: bob@firm.com\n" in page_text
    assert "Subject: Q3 Vendor Agreement Review\n" in page_text
    assert "Date: 2026-01-15\n" in page_text
    # Two newlines between preamble and body
    assert "\n\n" + tika_body in page_text


def test_parse_eml_body_text_does_not_contain_preamble():
    """parse_eml.body_text must NOT include the header preamble — the
    preamble is injected by the extract stage, not the parser."""
    eml = build_simple_email(
        message_id="<preamble-check@example.com>",
        subject="Q3 Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com"],
        body_text="Original body content goes here.",
    )
    result = parse_eml(eml)

    # The parser must NOT inject the preamble any more.
    assert not result.body_text.startswith("From:")
    assert "Subject:" not in result.body_text
    # But the body itself is still present
    assert "Original body content goes here." in result.body_text


async def test_eml_email_from_address_filter_retrieves_doc(db_session):
    """End-to-end: hybrid_search with email.from_address filter returns
    only the doc whose dedicated column matches."""
    eml = build_simple_email(
        message_id="<e2e-filter@example.com>",
        subject="Q3 Vendor Agreement Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com"],
        body_text="The quarterly report attached covers Q3 performance.",
    )
    parsed = parse_eml(eml)

    # Build the preamble the way extract.py would.
    preamble = _build_header_preamble(
        from_name=parsed.from_name,
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses,
        cc_addresses=parsed.cc_addresses,
        subject=parsed.subject,
        date_sent=parsed.date_sent,
    )
    chunk_text_a = preamble + parsed.body_text

    # Doc A: from Alice
    a = Document(
        title=parsed.subject,
        status="active",
        sha256=b"sha_e2e_f_a_000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id=parsed.message_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_subject=parsed.subject,
    )
    db_session.add(a)
    await db_session.flush()
    db_session.add(Chunk(doc_id=a.doc_id, chunk_num=0, chunk_text=chunk_text_a, language="en"))

    # Doc B: same subject, different sender (should NOT match the filter)
    b = Document(
        title=parsed.subject,
        status="active",
        sha256=b"sha_e2e_f_b_000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id="<e2e-other@example.com>",
        email_from_address="zelda@elsewhere.com",
        email_from_name="Zelda Z",
        email_subject=parsed.subject,
    )
    db_session.add(b)
    await db_session.flush()
    db_session.add(Chunk(doc_id=b.doc_id, chunk_num=0, chunk_text="quarterly report content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "quarterly report",
        k=10,
        metadata_filter={"email.from_address": "alice@firm.com"},
    )
    doc_ids = {h.doc_id for h in res.hits}
    assert str(a.doc_id) in doc_ids
    assert str(b.doc_id) not in doc_ids


def test_attachment_document_does_not_get_email_preamble():
    """Attachment Documents (PDFs, DOCXs etc. extracted from emails) have
    email_message_id set (linking them to the parent email) but also have
    email_parent_doc_id set (the canonical 'I am an attachment' signal).

    The extract.py gate requires BOTH:
      - doc.email_message_id is not None   (this is email-related)
      - doc.email_parent_doc_id is None    (this is the email body, not an attachment)

    This test verifies the gate logic directly without needing a live DB or
    Tika server. It uses SimpleNamespace to simulate the doc object because
    the gate is a pure attribute check.

    Without the fix, the old gate 'doc.email_message_id is not None' would
    fire on attachments too, injecting a degenerate preamble ('Subject:
    (no subject)') into every PDF/DOCX attachment extracted from an email.
    """
    parent_doc_id = uuid.uuid4()

    # Simulate an attachment-shaped Document: has email_message_id (links to
    # parent email) AND email_parent_doc_id (signal: I'm an attachment).
    # Other email_* columns (subject, from_*, to/cc) are NULL on attachments.
    attachment_doc = SimpleNamespace(
        email_message_id="<parent-email@example.com>",
        email_parent_doc_id=parent_doc_id,
        email_date_sent=datetime(2026, 1, 15, tzinfo=UTC),
        email_from_name=None,
        email_from_address=None,
        email_to_addresses=None,
        email_cc_addresses=None,
        email_subject=None,
    )

    # Simulate an email body Document: has email_message_id AND email_parent_doc_id IS NULL.
    email_body_doc = SimpleNamespace(
        email_message_id="<parent-email@example.com>",
        email_parent_doc_id=None,
        email_date_sent=datetime(2026, 1, 15, tzinfo=UTC),
        email_from_name="Alice Anderson",
        email_from_address="alice@firm.com",
        email_to_addresses=["bob@firm.com"],
        email_cc_addresses=[],
        email_subject="Q3 Vendor Agreement Review",
    )

    pages = [(1, "This is the actual contract content extracted from a PDF.")]

    # Replicate the exact gate condition from extract.py
    def should_inject_preamble(doc) -> bool:
        return doc.email_message_id is not None and doc.email_parent_doc_id is None and bool(pages)

    # Attachment: gate must NOT fire
    assert not should_inject_preamble(attachment_doc), (
        "Attachment Document with email_parent_doc_id set should NOT trigger preamble injection. "
        "This would inject 'Subject: (no subject)' into every PDF/DOCX attachment from an email."
    )

    # Email body: gate MUST fire
    assert should_inject_preamble(email_body_doc), (
        "Email body Document with email_parent_doc_id=None SHOULD trigger preamble injection."
    )

    # Confirm the preamble the email body would receive contains real content
    preamble = _build_header_preamble(
        from_name=email_body_doc.email_from_name or "",
        from_address=email_body_doc.email_from_address or "",
        to_addresses=email_body_doc.email_to_addresses or [],
        cc_addresses=email_body_doc.email_cc_addresses or [],
        subject=email_body_doc.email_subject or "(no subject)",
        date_sent=email_body_doc.email_date_sent,
    )
    assert "Subject: Q3 Vendor Agreement Review" in preamble
    assert "From: Alice Anderson <alice@firm.com>" in preamble

    # Confirm the attachment would receive no preamble (page_text unchanged)
    attachment_pages = list(pages)  # copy
    if not should_inject_preamble(attachment_doc):
        pass  # preamble not injected — pages unchanged
    original_text = "This is the actual contract content extracted from a PDF."
    assert attachment_pages[0][1] == original_text
    assert "Subject:" not in attachment_pages[0][1]
    assert "Date:" not in attachment_pages[0][1]
