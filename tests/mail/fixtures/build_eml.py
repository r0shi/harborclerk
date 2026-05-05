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
