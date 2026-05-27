"""End-to-end: parse a real-shaped .eml, persist Document + chunks, then
verify the header preamble lives in chunk 0 AND the email.* filter
namespace returns the right doc.

Critical Fix #1 — preamble injection lives in the extract stage, not in
parse_eml. These tests drive the extract-stage helper (_build_header_preamble
+ the injection logic) directly, without requiring a live Tika server.
"""

from datetime import UTC, datetime

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
