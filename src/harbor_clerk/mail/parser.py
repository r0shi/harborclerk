"""RFC 5322 email parsing → EmailParseResult.

Stage 3 producer side: take the raw .eml bytes (fetched via IMAP) and
extract everything the email Document and attachment Documents need —
metadata headers, body text, and attachment bytes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime


@dataclass(frozen=True)
class AttachmentSpec:
    """One attachment from an email — bytes + metadata."""

    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class EmailParseResult:
    """Parsed email ready to become Document(s)."""

    message_id: str
    subject: str
    from_address: str
    from_name: str
    to_addresses: list[str] = field(default_factory=list)
    cc_addresses: list[str] = field(default_factory=list)
    date_sent: datetime | None = None
    thread_id: str | None = None
    body_text: str = ""
    attachments: list[AttachmentSpec] = field(default_factory=list)


def parse_eml(eml_bytes: bytes) -> EmailParseResult:
    """Parse raw .eml bytes into a structured result.

    Best-effort: missing or malformed headers fall back to sensible
    defaults rather than raising. The only thing we always need is
    SOMETHING for `message_id` (synthesized from a SHA if header absent).
    """
    msg: Message = message_from_bytes(eml_bytes)
    message_id = msg.get("Message-ID") or msg.get("Message-Id") or _synthesize_message_id(eml_bytes)
    raw_subject = msg.get("Subject")
    if raw_subject:
        subject = str(make_header(decode_header(raw_subject)))
    else:
        subject = "(no subject)"
    sender_header = msg.get("From") or ""
    from_name, from_address = parseaddr(sender_header)
    to_addresses = _parse_addresses(msg.get_all("To") or [])
    cc_addresses = _parse_addresses(msg.get_all("Cc") or [])
    date_sent = _parse_date(msg.get("Date"))
    thread_id = msg.get("X-GM-THRID") or _thread_id_from_references(msg)
    body_text = _extract_body_text(msg)
    return EmailParseResult(
        message_id=message_id,
        subject=subject,
        from_address=from_address,
        from_name=from_name,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        date_sent=date_sent,
        thread_id=thread_id,
        body_text=body_text,
        attachments=_extract_attachments(msg),
    )


def _extract_attachments(msg: Message) -> list[AttachmentSpec]:
    """Walk the multipart tree and collect Content-Disposition: attachment parts.

    Inline images and other non-attachment parts are skipped per spec —
    Stage 3 only ingests parts the sender deliberately attached.
    """
    attachments: list[AttachmentSpec] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if not _is_attachment(part):
            continue
        content = part.get_payload(decode=True)
        if content is None:
            continue
        filename = part.get_filename() or "attachment"
        mime_type = part.get_content_type() or "application/octet-stream"
        attachments.append(
            AttachmentSpec(
                filename=filename,
                mime_type=mime_type,
                content=content,
            )
        )
    return attachments


def _parse_addresses(headers: list[str]) -> list[str]:
    """Extract just email addresses from one or more To/Cc header lines."""
    parsed = getaddresses(headers)
    return [addr for _name, addr in parsed if addr]


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _thread_id_from_references(msg: Message) -> str | None:
    """Use References: or In-Reply-To: as a thread id when X-GM-THRID
    isn't present. The first reference is the root of the thread."""
    refs = msg.get("References") or msg.get("In-Reply-To")
    if not refs:
        return None
    first = refs.split()[0].strip()
    return first if first.startswith("<") and first.endswith(">") else None


def _extract_body_text(msg: Message) -> str:
    """Pull the best-available text representation of the body.

    Prefer text/plain; fall back to text/html stripped of tags via a
    minimal regex. Future: consider html2text for higher fidelity, but
    Tika downstream will get a second crack at the .eml anyway.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not _is_attachment(part):
                return _decode_part(part)
        # Fallback: html
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not _is_attachment(part):
                return _strip_html(_decode_part(part))
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _is_attachment(part: Message) -> bool:
    return (part.get("Content-Disposition") or "").lower().startswith("attachment")


def _strip_html(html: str) -> str:
    """Minimal HTML stripper — drop tags, preserve text. For real
    rendering we delegate to Tika downstream."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _synthesize_message_id(eml_bytes: bytes) -> str:
    """Stable Message-ID derived from .eml content for messages with no
    header. The digest covers the full bytes, so the same email always
    produces the same id even across labels."""
    h = hashlib.sha256(eml_bytes).hexdigest()[:16]
    return f"<synthetic-{h}@harborclerk.local>"


def sanitize_subject_for_filename(subject: str) -> str:
    """Make a subject safe to use as a filesystem path component.

    - Replaces path separators (/ and \\) and control characters with `_`.
    - Truncates to 200 chars (filesystem limits + sanity).
    - Preserves Unicode (storage backends handle UTF-8 paths fine).
    - Empty or whitespace-only input → 'untitled'.
    """
    if not subject or not subject.strip():
        return "untitled"
    # Replace path separators and any control char (0x00-0x1F)
    cleaned = re.sub(r"[/\\\x00-\x1f]", "_", subject)
    # Collapse runs of whitespace into single spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200] if cleaned else "untitled"
