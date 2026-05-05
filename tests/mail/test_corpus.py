"""Test corpus: cover common real-world .eml shapes.

The fixtures here are programmatically built (not checked-in raw .eml
files) for the same reason as the parser tests — self-documenting + easy
to reason about. The shapes covered:

  - Plain text email (most common)
  - Multipart/alternative (text + html)
  - Multipart/mixed with attachments
  - HTML-only with inline image (inline NOT extracted as attachment)
  - Threaded reply (References header → thread_id)
"""

from email.message import EmailMessage
from email.utils import formatdate

from harbor_clerk.mail.parser import parse_eml


def test_corpus_plain_text_only():
    msg = EmailMessage()
    msg["Message-ID"] = "<corpus-plain@example.com>"
    msg["Subject"] = "Status update"
    msg["From"] = "Alice <alice@firm.com>"
    msg["To"] = "team@firm.com"
    msg["Date"] = formatdate(usegmt=True)
    msg.set_content("Quick status update.\n\nThings are good.\n")
    result = parse_eml(msg.as_bytes())
    assert "Things are good" in result.body_text
    assert result.attachments == []
    assert result.from_address == "alice@firm.com"


def test_corpus_multipart_alternative_text_and_html():
    msg = EmailMessage()
    msg["Message-ID"] = "<corpus-alt@example.com>"
    msg["Subject"] = "Newsletter"
    msg["From"] = "marketing@firm.com"
    msg["To"] = "user@example.com"
    msg.set_content("PLAIN newsletter content")
    msg.add_alternative("<html><body><p>HTML newsletter</p></body></html>", subtype="html")
    result = parse_eml(msg.as_bytes())
    assert "PLAIN newsletter" in result.body_text
    assert "<html>" not in result.body_text  # we picked text/plain
    assert result.attachments == []


def test_corpus_html_only_with_inline_image():
    """HTML-only email with an inline image. Body should be HTML-stripped;
    inline image must NOT show up in attachments."""
    msg = EmailMessage()
    msg["Message-ID"] = "<corpus-html@example.com>"
    msg["Subject"] = "Receipt"
    msg["From"] = "noreply@vendor.com"
    msg["To"] = "buyer@firm.com"
    # Set HTML as the only body
    msg.add_alternative("<html><body>Receipt for <b>$50</b></body></html>", subtype="html")
    msg.add_attachment(
        b"PNG-bytes-here",
        maintype="image",
        subtype="png",
        filename="logo.png",
        disposition="inline",
        cid="logo",
    )
    result = parse_eml(msg.as_bytes())
    assert "Receipt for" in result.body_text
    assert "$50" in result.body_text
    assert "<html>" not in result.body_text  # tags stripped
    assert result.attachments == []  # inline image excluded


def test_corpus_multipart_mixed_with_attachments():
    msg = EmailMessage()
    msg["Message-ID"] = "<corpus-mix@example.com>"
    msg["Subject"] = "Contract"
    msg["From"] = "legal@firm.com"
    msg["To"] = "counterparty@example.com"
    msg.set_content("Please sign and return.")
    msg.add_attachment(
        b"%PDF-1.4 contract bytes",
        maintype="application",
        subtype="pdf",
        filename="contract.pdf",
    )
    msg.add_attachment(
        b"PK signature DOCX bytes",
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="addendum.docx",
    )
    result = parse_eml(msg.as_bytes())
    assert "Please sign" in result.body_text
    assert len(result.attachments) == 2
    filenames = sorted(a.filename for a in result.attachments)
    assert filenames == ["addendum.docx", "contract.pdf"]


def test_corpus_threaded_reply_uses_references_header():
    """A reply email has References: <root-id> ... — use that as thread_id."""
    msg = EmailMessage()
    msg["Message-ID"] = "<reply@example.com>"
    msg["Subject"] = "Re: Q3 plan"
    msg["From"] = "alice@firm.com"
    msg["To"] = "bob@firm.com"
    msg["References"] = "<root@example.com> <intermediate@example.com>"
    msg["In-Reply-To"] = "<intermediate@example.com>"
    msg.set_content("Sounds good.")
    result = parse_eml(msg.as_bytes())
    assert result.thread_id == "<root@example.com>"
