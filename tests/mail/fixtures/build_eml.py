"""Helpers to build synthetic .eml bytes for tests.

Building messages programmatically (rather than checking in raw .eml
fixtures) keeps tests self-documenting and lets us cover edge cases
(missing Message-ID, multipart, attachments, encoded headers) without
hunting for real-world examples.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate


def build_simple_email(
    *,
    message_id: str | None = "<simple@example.com>",
    subject: str = "Test subject",
    sender: str = "Alice <alice@example.com>",
    recipients: list[str] | None = None,
    cc: list[str] | None = None,
    date: str | None = None,
    body_text: str = "Hello world.",
) -> bytes:
    """Build a single-part text/plain email and return its raw bytes."""
    msg = EmailMessage()
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg["Subject"] = subject
    msg["From"] = sender
    if recipients is None:
        recipients = ["bob@example.com"]
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = date or formatdate(localtime=False, usegmt=True)
    msg.set_content(body_text)
    return msg.as_bytes()


def build_email_with_attachments(
    *,
    message_id: str | None = "<with-attach@example.com>",
    subject: str = "See attached",
    sender: str = "alice@example.com",
    body_text: str = "Body text.",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Build a multipart/mixed email with N attachments.

    `attachments` is a list of (filename, mime_type, content) tuples.
    """
    msg = EmailMessage()
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "bob@example.com"
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg.set_content(body_text)
    for filename, mime_type, content in attachments or []:
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    return msg.as_bytes()
