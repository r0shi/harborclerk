"""Tests for email .eml parsing."""

from datetime import datetime

from harbor_clerk.mail.parser import parse_eml
from tests.mail.fixtures.build_eml import build_email_with_attachments, build_simple_email


def test_parse_minimal_message_id_subject_body():
    eml = build_simple_email(
        message_id="<abc@example.com>",
        subject="Q3 Vendor Agreement",
        sender="Alice <alice@example.com>",
        body_text="Please review the attached contract.",
    )
    result = parse_eml(eml)
    assert result.message_id == "<abc@example.com>"
    assert result.subject == "Q3 Vendor Agreement"
    assert "Please review" in result.body_text


def test_parse_extracts_sender_name_and_address():
    eml = build_simple_email(sender="Alice Anderson <alice@firm.com>")
    result = parse_eml(eml)
    assert result.from_address == "alice@firm.com"
    assert result.from_name == "Alice Anderson"


def test_parse_extracts_recipients():
    eml = build_simple_email(
        recipients=["bob@firm.com", "carol@firm.com"],
        cc=["legal@firm.com"],
    )
    result = parse_eml(eml)
    assert "bob@firm.com" in result.to_addresses
    assert "carol@firm.com" in result.to_addresses
    assert "legal@firm.com" in result.cc_addresses


def test_parse_extracts_date_as_aware_datetime():
    eml = build_simple_email(date="Fri, 30 Apr 2026 14:23:00 +0000")
    result = parse_eml(eml)
    assert isinstance(result.date_sent, datetime)
    assert result.date_sent.tzinfo is not None
    assert result.date_sent.year == 2026
    assert result.date_sent.month == 4
    assert result.date_sent.day == 30


def test_parse_no_attachments_for_text_only():
    eml = build_simple_email()
    result = parse_eml(eml)
    assert result.attachments == []


def test_parse_attachment_extracts_bytes():
    pdf_bytes = b"%PDF-1.4 fake pdf content for test"
    eml = build_email_with_attachments(
        attachments=[("contract.pdf", "application/pdf", pdf_bytes)],
    )
    result = parse_eml(eml)
    assert len(result.attachments) == 1
    att = result.attachments[0]
    assert att.filename == "contract.pdf"
    assert att.mime_type == "application/pdf"
    assert att.content == pdf_bytes


def test_parse_multiple_attachments_preserves_order():
    eml = build_email_with_attachments(
        attachments=[
            ("a.pdf", "application/pdf", b"a-content"),
            ("b.txt", "text/plain", b"b-content"),
            ("c.jpg", "image/jpeg", b"c-content"),
        ],
    )
    result = parse_eml(eml)
    filenames = [a.filename for a in result.attachments]
    assert filenames == ["a.pdf", "b.txt", "c.jpg"]


def test_parse_inline_image_is_NOT_an_attachment():
    """Inline images (Content-Disposition: inline) are skipped per spec.
    Only Content-Disposition: attachment counts."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Message-ID"] = "<inline@example.com>"
    msg["Subject"] = "With inline image"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg.set_content("Body text.")
    msg.add_attachment(
        b"fake-png-bytes",
        maintype="image",
        subtype="png",
        filename="signature.png",
        disposition="inline",
    )
    result = parse_eml(msg.as_bytes())
    assert result.attachments == []  # inline image is NOT collected
    assert "Body text" in result.body_text


def test_parse_body_prefers_text_plain_over_html():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Message-ID"] = "<alt@example.com>"
    msg["Subject"] = "alt"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg.set_content("PLAIN body")
    msg.add_alternative("<p>HTML body</p>", subtype="html")
    result = parse_eml(msg.as_bytes())
    assert "PLAIN body" in result.body_text
    assert "HTML body" not in result.body_text


def test_parse_no_message_id_synthesizes_stable_id():
    """An email with no Message-ID header gets a deterministic synthetic id
    derived from its bytes — same email always produces the same id."""
    eml = build_simple_email(message_id=None, subject="No ID")
    result1 = parse_eml(eml)
    result2 = parse_eml(eml)
    assert result1.message_id == result2.message_id
    assert result1.message_id.startswith("<synthetic-")
    assert result1.message_id.endswith("@harborclerk.local>")


def test_parse_synthesized_message_ids_differ_per_message():
    eml1 = build_simple_email(message_id=None, subject="A")
    eml2 = build_simple_email(message_id=None, subject="B")
    r1 = parse_eml(eml1)
    r2 = parse_eml(eml2)
    assert r1.message_id != r2.message_id


def test_parse_encoded_subject_decodes_to_unicode():
    """Subjects passed as Unicode get RFC 2047 encoded by EmailMessage,
    parse_eml should decode back to the original Unicode."""
    eml = build_simple_email(subject="日本語の件名")
    result = parse_eml(eml)
    assert result.subject == "日本語の件名"


def test_parse_missing_subject_falls_back_to_no_subject_marker():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Message-ID"] = "<no-subject@example.com>"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg.set_content("Body.")
    result = parse_eml(msg.as_bytes())
    assert result.subject == "(no subject)"


def test_parse_empty_body_returns_empty_string():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Message-ID"] = "<empty@example.com>"
    msg["Subject"] = "empty"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    # Don't call set_content — body is empty
    result = parse_eml(msg.as_bytes())
    assert result.body_text == ""
