"""Tests for email .eml parsing."""

from datetime import datetime

from harbor_clerk.mail.parser import parse_eml
from tests.mail.fixtures.build_eml import build_simple_email


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
