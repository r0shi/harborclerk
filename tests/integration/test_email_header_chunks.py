"""End-to-end: parse a real-shaped .eml, persist Document + chunks, then
verify the header preamble lives in chunk 0 AND the email.* filter
namespace returns the right doc."""

from harbor_clerk.mail.parser import parse_eml
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search
from tests.mail.fixtures.build_eml import build_simple_email


async def test_eml_chunk_0_contains_header_preamble(db_session):
    """parse_eml + persist + chunk → chunk 0's text contains header preamble."""
    eml = build_simple_email(
        message_id="<e2e-headers@example.com>",
        subject="Q3 Vendor Agreement Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com"],
        body_text="The quarterly report attached covers Q3 performance.",
    )
    parsed = parse_eml(eml)

    doc = Document(
        title=parsed.subject,
        status="active",
        sha256=b"sha_e2e_h_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id=parsed.message_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_to_addresses=parsed.to_addresses,
        email_subject=parsed.subject,
        email_date_sent=parsed.date_sent,
    )
    db_session.add(doc)
    await db_session.flush()
    # Single chunk holding the full body_text (preamble + body), simulating
    # what the real chunker does for a short body.
    db_session.add(
        Chunk(
            doc_id=doc.doc_id,
            chunk_num=0,
            chunk_text=parsed.body_text,
            language="en",
        )
    )
    await db_session.flush()

    # Header preamble lines all present in the chunk text
    assert "From: Alice Anderson <alice@firm.com>" in parsed.body_text
    assert "To: bob@firm.com" in parsed.body_text
    assert "Subject: Q3 Vendor Agreement Review" in parsed.body_text
    # And the body
    assert "quarterly report" in parsed.body_text


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
    db_session.add(Chunk(doc_id=a.doc_id, chunk_num=0, chunk_text=parsed.body_text, language="en"))

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
