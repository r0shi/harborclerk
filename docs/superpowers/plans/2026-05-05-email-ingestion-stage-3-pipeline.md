# Email Ingestion — Stage 3: Email → Document Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert each `watched_messages` row that Stage 2's sync engine produces into a real email Document plus N attachment Documents, save the original `.eml` and attachment bytes to storage, and enqueue the existing `extract` stage so the pipeline runs end-to-end. Also handle lifecycle: when sync transitions a `watched_message` to `unlabeled`, soft-delete its associated Documents.

**Architecture:** New `harbor_clerk/mail/parser.py` parses `.eml` bytes via Python's stdlib `email` module into a typed `EmailParseResult` (metadata + body + attachments). New `harbor_clerk/mail/ingest.py` orchestrates: read `watched_messages` with `email_doc_id IS NULL` → FETCH `BODY.PEEK[]` → compute SHA → cross-label dedup → parse → save originals via the existing `StorageBackend` → create email + attachment Documents → enqueue `extract`. Lifecycle additions in `harbor_clerk/mail/lifecycle.py` soft-delete Documents when their watched_messages transition to `unlabeled` (and restore on re-label). The `MailObserver` invokes ingest after each sync inside its `on_tick` callback.

**Tech Stack:** Python 3.12, stdlib `email.parser.BytesParser` (RFC 5322 parsing), the existing `StorageBackend` (MinIO on Docker, Filesystem on macOS), the existing `enqueue_stage()` from `worker/pipeline.py`, SQLAlchemy 2.0 async, asyncio.

**Spec:** [`docs/superpowers/specs/2026-05-04-email-ingestion-design.md`](../specs/2026-05-04-email-ingestion-design.md)

**Builds on:** Stage 2 ([PR #282](https://github.com/r0shi/harborclerk/pull/282)) — `MailObserver`, IMAP client, sync engine. Plus Stage 1 ([PR #281](https://github.com/r0shi/harborclerk/pull/281)) — schema, models, secrets.

**Implementation note:** Tika already extracts `message/rfc822` text content (verified at `worker/stages/extract.py:369`); attachments inherit the existing per-mime extract path. So Stage 3's job is purely the *producer* side — creating Documents from emails. The downstream pipeline (extract → chunk → entities → embed → summarize → finalize) runs unchanged. After Stage 3 lands, an admin who has an account configured (via Stage 2's API) and a label being watched will see new emails appear as Documents in the existing Documents list.

---

## File Structure

**New files:**
- `src/harbor_clerk/mail/parser.py` — `EmailParseResult`, `AttachmentSpec`, `parse_eml(eml_bytes) -> EmailParseResult`
- `src/harbor_clerk/mail/ingest.py` — `ingest_pending_messages(session, conn, label) -> IngestSummary`
- `src/harbor_clerk/mail/document_lifecycle.py` — `soft_delete_documents_for_unlabeled(session, label) -> int`, `restore_documents_for_relabeled(session, label) -> int`
- `tests/mail/test_parser.py`
- `tests/mail/test_ingest.py`
- `tests/mail/test_document_lifecycle.py`
- `tests/mail/fixtures/__init__.py` — empty
- `tests/mail/fixtures/build_eml.py` — helper that builds synthetic `.eml` bytes for tests (simpler than checking in raw .eml files; lets tests be self-documenting)

**Modified files:**
- `src/harbor_clerk/mail/__init__.py` — export `EmailParseResult`, `parse_eml`, `ingest_pending_messages`
- `src/harbor_clerk/mail/sync.py` — extract `_synthesize_message_id` to `parser.py` and re-import (avoid duplication)
- `src/harbor_clerk/watcher/mail_observer.py` — call `ingest_pending_messages` after each sync in `on_tick`; call lifecycle handlers after `detect_unlabeled_messages`

---

## Task 1: Email parser — minimal headers + plain-text body

**Files:**
- Create: `src/harbor_clerk/mail/parser.py`
- Create: `tests/mail/fixtures/__init__.py`
- Create: `tests/mail/fixtures/build_eml.py`
- Create: `tests/mail/test_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/fixtures/__init__.py — empty
```

```python
# tests/mail/fixtures/build_eml.py
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
```

```python
# tests/mail/test_parser.py
"""Tests for email .eml parsing."""

from datetime import UTC, datetime

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: FAIL — `harbor_clerk.mail.parser` doesn't exist.

- [ ] **Step 3: Implement `parser.py`**

```python
# src/harbor_clerk/mail/parser.py
"""RFC 5322 email parsing → EmailParseResult.

Stage 3 producer side: take the raw .eml bytes (fetched via IMAP) and
extract everything the email Document and attachment Documents need —
metadata headers, body text, and attachment bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
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
    subject = msg.get("Subject") or "(no subject)"
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
        attachments=[],  # Task 3 fills this
    )


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
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _synthesize_message_id(eml_bytes: bytes) -> str:
    """Stable Message-ID derived from .eml content for messages with no
    header. The digest covers the full bytes, so the same email always
    produces the same id even across labels."""
    h = hashlib.sha256(eml_bytes).hexdigest()[:16]
    return f"<synthetic-{h}@harborclerk.local>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/parser.py tests/mail/fixtures/ tests/mail/test_parser.py
git commit -m "feat(mail): EmailParseResult + parse_eml — minimal headers + body extraction"
```

---

## Task 2: Parser — multipart with attachments

**Files:**
- Modify: `src/harbor_clerk/mail/parser.py`
- Modify: `tests/mail/fixtures/build_eml.py`
- Modify: `tests/mail/test_parser.py`

- [ ] **Step 1: Append attachment-builder to the fixture helper**

Append to `tests/mail/fixtures/build_eml.py`:

```python
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
```

- [ ] **Step 2: Append the failing tests**

Append to `tests/mail/test_parser.py`:

```python
from tests.mail.fixtures.build_eml import build_email_with_attachments


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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: 5 from Task 1 PASS, 4 new tests FAIL — `result.attachments` is empty (Task 1's `parse_eml` returns `attachments=[]`).

- [ ] **Step 4: Implement attachment extraction**

In `src/harbor_clerk/mail/parser.py`, replace `parse_eml`'s `attachments=[]` line with attachment-collection logic. Add a new helper:

```python
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
```

Update `parse_eml`'s return to call this:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: PASS — all 9 tests green.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mail/parser.py tests/mail/fixtures/build_eml.py tests/mail/test_parser.py
git commit -m "feat(mail): parse attachments — Content-Disposition: attachment only"
```

---

## Task 3: Parser — edge cases (no Message-ID, encoded headers, no body)

**Files:**
- Modify: `tests/mail/test_parser.py`

The implementation from Tasks 1-2 already handles these via the synthesized fallback and best-effort extraction; this task adds tests to lock the behavior.

- [ ] **Step 1: Append the failing/passing tests**

Append to `tests/mail/test_parser.py`:

```python
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
    """RFC 2047 encoded-word subjects (=?utf-8?B?...?=) decode to Unicode."""
    eml = build_simple_email(subject="日本語の件名").replace(
        b"Subject: =?utf-8?", b"Subject: =?utf-8?"
    )
    # Easier: build the email with a Unicode subject directly. EmailMessage
    # encodes it as RFC 2047 automatically; parse_eml should decode it back.
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
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: most PASS. The `test_parse_encoded_subject_decodes_to_unicode` test may FAIL if `parse_eml` doesn't explicitly decode RFC 2047 — Python's `email.message_from_bytes` returns subjects as `str` already decoded in recent versions, but check.

- [ ] **Step 3: If encoded-subject test fails, fix `parse_eml`**

If the encoded-subject test fails, add an explicit decode step. In `parse_eml`, replace:

```python
subject = msg.get("Subject") or "(no subject)"
```

with:

```python
from email.header import decode_header, make_header
raw_subject = msg.get("Subject")
if raw_subject:
    subject = str(make_header(decode_header(raw_subject)))
else:
    subject = "(no subject)"
```

(Move the `from email.header import ...` to the top of the file with the other imports.)

- [ ] **Step 4: Run tests again**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: PASS — all 14 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/parser.py tests/mail/test_parser.py
git commit -m "test(mail): parser edge cases — no-id, encoded headers, missing subject/body"
```

---

## Task 4: Subject sanitization for storage filenames

**Files:**
- Modify: `src/harbor_clerk/mail/parser.py`
- Modify: `tests/mail/test_parser.py`

The email Document stores its `.eml` at `originals/<doc_id>/<safe_subject>.eml`. Subject text can contain `/`, control characters, or be very long — we need a sanitizer.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_parser.py`:

```python
from harbor_clerk.mail.parser import sanitize_subject_for_filename


def test_sanitize_subject_replaces_path_separators():
    assert sanitize_subject_for_filename("docs/2026/Q3 contract") == "docs_2026_Q3 contract"
    assert sanitize_subject_for_filename("a\\b\\c") == "a_b_c"


def test_sanitize_subject_strips_control_characters():
    assert sanitize_subject_for_filename("hello\x00world\nfoo") == "hello_world_foo"


def test_sanitize_subject_truncates_long_strings():
    long_subject = "x" * 500
    result = sanitize_subject_for_filename(long_subject)
    assert len(result) <= 200


def test_sanitize_subject_preserves_unicode():
    assert sanitize_subject_for_filename("日本語の件名") == "日本語の件名"


def test_sanitize_subject_handles_empty_input():
    assert sanitize_subject_for_filename("") == "untitled"
    assert sanitize_subject_for_filename("   ") == "untitled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_parser.py::test_sanitize_subject_replaces_path_separators -v`
Expected: FAIL — `sanitize_subject_for_filename` doesn't exist.

- [ ] **Step 3: Implement the sanitizer**

Append to `src/harbor_clerk/mail/parser.py`:

```python
def sanitize_subject_for_filename(subject: str) -> str:
    """Make a subject safe to use as a filesystem path component.

    - Replaces path separators (/ and \\) and control characters with `_`.
    - Truncates to 200 chars (filesystem limits + sanity).
    - Preserves Unicode (storage backends handle UTF-8 paths fine).
    - Empty or whitespace-only input → 'untitled'.
    """
    import re

    if not subject or not subject.strip():
        return "untitled"
    # Replace path separators and any control char (0x00-0x1F)
    cleaned = re.sub(r"[/\\\x00-\x1f]", "_", subject)
    # Collapse runs of whitespace into single spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200] if cleaned else "untitled"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_parser.py -v`
Expected: PASS — all 19 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/parser.py tests/mail/test_parser.py
git commit -m "feat(mail): sanitize_subject_for_filename — path-safe storage filenames"
```

---

## Task 5: Ingest — fetch full .eml bytes from IMAP

**Files:**
- Create: `src/harbor_clerk/mail/ingest.py`
- Create: `tests/mail/test_ingest.py`

The sync engine inserts `watched_messages` rows with placeholder `eml_sha256`. Stage 3's ingest fetches the actual `.eml` via IMAP `BODY.PEEK[]`, computes the real SHA, and updates the row. Cross-label dedup checks for an existing Document with the same SHA before parsing.

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_ingest.py
"""Tests for the email-ingest pipeline (watched_messages → Documents)."""

import hashlib

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.ingest import fetch_eml_bytes
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_fetch_eml_bytes_returns_raw_message(mock_aioimap, watched_label):
    eml = b"From: a@example.com\r\nSubject: t\r\n\r\nBody"
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response("OK", [
        b"1 (UID 1 BODY[] {%d}" % len(eml),
        eml,
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    fetched = await fetch_eml_bytes(conn, uid=1)
    await conn.logout()

    assert fetched == eml
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: FAIL — `harbor_clerk.mail.ingest` doesn't exist.

- [ ] **Step 3: Implement `fetch_eml_bytes`**

```python
# src/harbor_clerk/mail/ingest.py
"""Stage 3: turn watched_messages into Documents.

The sync engine (Stage 2) inserts a watched_message row with a placeholder
eml_sha256 each time it discovers a UID. Stage 3 picks up those rows and:
  1. Fetches the full .eml bytes via IMAP BODY.PEEK[].
  2. Computes the real SHA-256 of the bytes.
  3. Cross-label dedup: if any other watched_message already references a
     Document with this SHA, link to that existing Document.
  4. Otherwise: parse, save originals to storage, create email Document
     plus N attachment Documents, enqueue extract on each.
  5. Update the watched_message: real eml_sha256 + email_doc_id.

Documents are NOT deleted from this module — the lifecycle handler in
`document_lifecycle.py` does that on watched_message status transitions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from harbor_clerk.mail.imap_client import IMAPConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestSummary:
    """What an ingest invocation did."""

    fetched_count: int
    new_email_doc_count: int
    new_attachment_doc_count: int
    deduped_count: int  # rows that linked to an existing Document via SHA match


async def fetch_eml_bytes(conn: IMAPConnection, uid: int) -> bytes:
    """FETCH UID <uid> BODY.PEEK[] and return the raw RFC 5322 bytes.

    BODY.PEEK[] is BODY[] without setting the \\Seen flag — important for
    a read-only sync engine that shouldn't mutate the user's mailbox.
    """
    result, lines = await conn.client.uid("FETCH", str(uid), "BODY.PEEK[]")
    if result != "OK":
        raise RuntimeError(f"FETCH UID {uid} failed: {result}")
    return _extract_literal(lines)


def _extract_literal(fetch_lines: list[bytes]) -> bytes:
    """Pull the RFC 5322 message bytes out of a FETCH response.

    The structural line ends with `{<length>}` and the next N bytes are
    the message body. We collect the body bytes until we hit the
    closing `)` line.
    """
    body_chunks: list[bytes] = []
    expect_body = False
    for line in fetch_lines:
        if expect_body:
            if line == b")" or line.startswith(b")"):
                break
            body_chunks.append(line)
            continue
        # Structural line: `1 (UID 1 BODY[] {1234}` — after this, the message body lines follow
        if re.search(rb"\{\d+\}\s*$", line):
            expect_body = True
    # Reconstruct: aioimaplib splits the literal on \r\n; we need to rejoin
    return b"\r\n".join(body_chunks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: PASS — 1 test green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/ingest.py tests/mail/test_ingest.py
git commit -m "feat(mail): fetch_eml_bytes — pull full .eml via IMAP BODY.PEEK[]"
```

---

## Task 6: Ingest — create email Document

**Files:**
- Modify: `src/harbor_clerk/mail/ingest.py`
- Modify: `tests/mail/test_ingest.py`

Given a parsed `EmailParseResult` and the `watched_message` row, create the email Document and save the `.eml` to storage.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_ingest.py`:

```python
import io
from datetime import UTC, datetime

from harbor_clerk.mail.ingest import create_email_document
from harbor_clerk.mail.parser import EmailParseResult
from harbor_clerk.models import Document
from harbor_clerk.storage import get_storage


async def test_create_email_document_persists_metadata(db_session, watched_label):
    parsed = EmailParseResult(
        message_id="<email1@example.com>",
        subject="Test email",
        from_address="alice@example.com",
        from_name="Alice",
        to_addresses=["bob@example.com"],
        cc_addresses=[],
        date_sent=datetime(2026, 4, 30, 14, 23, tzinfo=UTC),
        thread_id="thread-1",
        body_text="Body content",
    )
    eml_bytes = b"From: alice\r\nSubject: Test email\r\n\r\nBody content"
    eml_sha256 = b"\xab" * 32

    doc = await create_email_document(
        db_session,
        parsed=parsed,
        eml_bytes=eml_bytes,
        eml_sha256=eml_sha256,
        label=watched_label,
    )
    await db_session.flush()

    assert doc.title == "Test email"
    assert doc.email_message_id == "<email1@example.com>"
    assert doc.email_from_address == "alice@example.com"
    assert doc.email_from_name == "Alice"
    assert doc.email_to_addresses == ["bob@example.com"]
    assert doc.email_thread_id == "thread-1"
    assert doc.email_label_path == watched_label.label_path
    assert doc.email_date_sent == datetime(2026, 4, 30, 14, 23, tzinfo=UTC)
    # created_at should equal date_sent (per spec — sort by send date)
    assert doc.created_at == datetime(2026, 4, 30, 14, 23, tzinfo=UTC)
    assert doc.mime_type == "message/rfc822"
    assert doc.sha256 == eml_sha256
    # original_object_key set to a path under originals/<doc_id>/...
    assert doc.original_object_key is not None
    assert str(doc.doc_id) in doc.original_object_key
    assert doc.original_object_key.endswith(".eml")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_ingest.py::test_create_email_document_persists_metadata -v`
Expected: FAIL — `create_email_document` doesn't exist.

- [ ] **Step 3: Implement `create_email_document`**

Append to `src/harbor_clerk/mail/ingest.py`:

```python
import io
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.parser import EmailParseResult, sanitize_subject_for_filename
from harbor_clerk.models import Document, WatchedLabel
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.storage import get_storage


async def create_email_document(
    session: AsyncSession,
    *,
    parsed: EmailParseResult,
    eml_bytes: bytes,
    eml_sha256: bytes,
    label: WatchedLabel,
) -> Document:
    """Create the email Document, save the .eml to storage, return the row.

    Caller flushes/commits. Caller is responsible for also creating attachment
    Documents (Task 7) and updating the watched_message pointer.

    The Document's `created_at` is set to `email_date_sent` (per spec) so the
    Documents page sorts by send date, not ingest time.
    """
    doc_id = uuid.uuid4()
    safe_subject = sanitize_subject_for_filename(parsed.subject)
    storage_key = f"originals/{doc_id}/{safe_subject}.eml"

    storage = get_storage()
    storage.put_object(
        bucket="originals",
        key=storage_key,
        data=io.BytesIO(eml_bytes),
        length=len(eml_bytes),
        content_type="message/rfc822",
    )

    # Use the spec's send-date-as-created-at semantics. Fall back to now()
    # when the email had no Date header (rare).
    created_at = parsed.date_sent or datetime.now(UTC)

    doc = Document(
        doc_id=doc_id,
        title=parsed.subject,
        canonical_filename=f"{safe_subject}.eml",
        sha256=eml_sha256,
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        size_bytes=len(eml_bytes),
        original_object_key=storage_key,
        original_bucket="originals",
        email_message_id=parsed.message_id,
        email_thread_id=parsed.thread_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_to_addresses=parsed.to_addresses or None,
        email_cc_addresses=parsed.cc_addresses or None,
        email_date_sent=parsed.date_sent,
        email_label_path=label.label_path,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(doc)
    return doc
```

(The imports at the top of the file may need to be reorganized to top-of-file rather than inline — adjust per the existing style.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: PASS — 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/ingest.py tests/mail/test_ingest.py
git commit -m "feat(mail): create_email_document — persist Document + save .eml to storage"
```

---

## Task 7: Ingest — create attachment Documents

**Files:**
- Modify: `src/harbor_clerk/mail/ingest.py`
- Modify: `tests/mail/test_ingest.py`

For each attachment in the parsed email, create a Document linked to the email Document via `email_parent_doc_id`.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_ingest.py`:

```python
from harbor_clerk.mail.ingest import create_attachment_documents
from harbor_clerk.mail.parser import AttachmentSpec


async def test_create_attachment_documents_links_to_parent(db_session, watched_label):
    # Set up parent email Document
    parent = Document(
        title="Parent email",
        canonical_filename="parent.eml",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        email_message_id="<parent@example.com>",
        email_label_path=watched_label.label_path,
    )
    db_session.add(parent)
    await db_session.flush()

    attachments = [
        AttachmentSpec(filename="contract.pdf", mime_type="application/pdf", content=b"%PDF-1.4 fake"),
        AttachmentSpec(filename="addendum.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", content=b"PK fake docx"),
    ]
    parsed = EmailParseResult(
        message_id="<parent@example.com>",
        subject="Parent email",
        from_address="alice@example.com",
        from_name="Alice",
        date_sent=datetime(2026, 4, 30, tzinfo=UTC),
        attachments=attachments,
    )

    docs = await create_attachment_documents(
        db_session,
        parsed=parsed,
        parent_doc=parent,
        label=watched_label,
    )
    await db_session.flush()

    assert len(docs) == 2
    assert docs[0].title == "contract.pdf"
    assert docs[0].email_parent_doc_id == parent.doc_id
    assert docs[0].email_message_id == "<parent@example.com>"
    assert docs[0].mime_type == "application/pdf"
    assert docs[0].original_object_key.endswith("contract.pdf")
    # created_at inherits from parent's send date so attachments sort with their email
    assert docs[0].created_at == datetime(2026, 4, 30, tzinfo=UTC)

    assert docs[1].title == "addendum.docx"
    assert docs[1].email_parent_doc_id == parent.doc_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_ingest.py::test_create_attachment_documents_links_to_parent -v`
Expected: FAIL — `create_attachment_documents` doesn't exist.

- [ ] **Step 3: Implement `create_attachment_documents`**

Append to `src/harbor_clerk/mail/ingest.py`:

```python
import hashlib


async def create_attachment_documents(
    session: AsyncSession,
    *,
    parsed: EmailParseResult,
    parent_doc: Document,
    label: WatchedLabel,
) -> list[Document]:
    """Create one Document per parsed attachment. Bytes saved to storage,
    linked back to the parent email via email_parent_doc_id.

    Caller flushes/commits.
    """
    storage = get_storage()
    docs: list[Document] = []
    for attachment in parsed.attachments:
        doc_id = uuid.uuid4()
        # Use the original filename (sanitized to be path-safe) for storage
        safe_filename = sanitize_subject_for_filename(attachment.filename)
        storage_key = f"originals/{doc_id}/{safe_filename}"

        storage.put_object(
            bucket="originals",
            key=storage_key,
            data=io.BytesIO(attachment.content),
            length=len(attachment.content),
            content_type=attachment.mime_type,
        )

        sha = hashlib.sha256(attachment.content).digest()

        doc = Document(
            doc_id=doc_id,
            title=attachment.filename,
            canonical_filename=safe_filename,
            sha256=sha,
            pipeline_status=PipelineStatus.queued,
            mime_type=attachment.mime_type,
            size_bytes=len(attachment.content),
            original_object_key=storage_key,
            original_bucket="originals",
            email_parent_doc_id=parent_doc.doc_id,
            email_message_id=parsed.message_id,
            email_label_path=label.label_path,
            email_date_sent=parsed.date_sent,
            created_at=parsed.date_sent or datetime.now(UTC),
            updated_at=parsed.date_sent or datetime.now(UTC),
        )
        session.add(doc)
        docs.append(doc)
    return docs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: PASS — 3 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/ingest.py tests/mail/test_ingest.py
git commit -m "feat(mail): create_attachment_documents — one Document per attached part"
```

---

## Task 8: Ingest — orchestrator with cross-label dedup + extract enqueue

**Files:**
- Modify: `src/harbor_clerk/mail/ingest.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Modify: `tests/mail/test_ingest.py`

The top-level `ingest_pending_messages(session, conn, label)` orchestrates: find rows with `email_doc_id IS NULL`, fetch each, dedup by SHA across labels, parse, create Documents, enqueue extract.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_ingest.py`:

```python
from harbor_clerk.mail.ingest import IngestSummary, ingest_pending_messages
from tests.mail.fixtures.build_eml import build_email_with_attachments


async def test_ingest_creates_email_and_attachment_docs(db_session, watched_label, mock_aioimap):
    # Pre-populate a watched_message row (simulating Stage 2 sync output)
    eml = build_email_with_attachments(
        message_id="<full@example.com>",
        subject="Full ingest test",
        body_text="Body.",
        attachments=[("contract.pdf", "application/pdf", b"%PDF-1.4 fake")],
    )
    placeholder_sha = hashlib.sha256(b"placeholder").digest()
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<full@example.com>",
        imap_uid=42,
        eml_sha256=placeholder_sha,
        status="active",
        email_doc_id=None,  # not yet ingested
    )
    db_session.add(msg)
    await db_session.flush()

    # Mock IMAP FETCH to return the .eml bytes
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response("OK", [
        b"42 (UID 42 BODY[] {%d}" % len(eml),
        eml,
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 1
    assert summary.new_email_doc_count == 1
    assert summary.new_attachment_doc_count == 1
    assert summary.deduped_count == 0

    # watched_message now has real SHA + email_doc_id pointer
    await db_session.refresh(msg)
    assert msg.eml_sha256 != placeholder_sha
    assert msg.eml_sha256 == hashlib.sha256(eml).digest()
    assert msg.email_doc_id is not None

    # Email Document exists
    email_doc = (await db_session.execute(
        select(Document).where(Document.doc_id == msg.email_doc_id)
    )).scalar_one()
    assert email_doc.title == "Full ingest test"
    assert email_doc.mime_type == "message/rfc822"

    # Attachment Document exists, linked to email
    attachments = (await db_session.execute(
        select(Document).where(Document.email_parent_doc_id == msg.email_doc_id)
    )).scalars().all()
    assert len(attachments) == 1
    assert attachments[0].title == "contract.pdf"


async def test_ingest_dedupes_across_labels_via_sha(db_session, mail_account, mock_aioimap):
    """Same email already ingested via another label → reuse the existing
    email_doc_id without creating a new Document."""
    from harbor_clerk.models import WatchedLabel

    label_a = WatchedLabel(account_id=mail_account.account_id, label_path="LabelA", display_name="LabelA")
    label_b = WatchedLabel(account_id=mail_account.account_id, label_path="LabelB", display_name="LabelB")
    db_session.add_all([label_a, label_b])
    await db_session.flush()

    eml = build_email_with_attachments(
        message_id="<dup@example.com>", subject="Dup", body_text="x",
    )
    real_sha = hashlib.sha256(eml).digest()

    # Pre-populate label_a with an already-ingested watched_message
    parent_doc = Document(
        title="Dup",
        canonical_filename="Dup.eml",
        sha256=real_sha,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id="<dup@example.com>",
        original_object_key="originals/existing/dup.eml",
        original_bucket="originals",
    )
    db_session.add(parent_doc)
    await db_session.flush()
    msg_a = WatchedMessage(
        label_id=label_a.label_id, message_id="<dup@example.com>", imap_uid=1,
        eml_sha256=real_sha, status="active", email_doc_id=parent_doc.doc_id,
    )
    db_session.add(msg_a)

    # Now label_b discovers the same message (placeholder SHA)
    placeholder_sha = hashlib.sha256(b"placeholder-b").digest()
    msg_b = WatchedMessage(
        label_id=label_b.label_id, message_id="<dup@example.com>", imap_uid=99,
        eml_sha256=placeholder_sha, status="active", email_doc_id=None,
    )
    db_session.add(msg_b)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response("OK", [
        b"99 (UID 99 BODY[] {%d}" % len(eml),
        eml,
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, label_b)
    await conn.logout()
    await db_session.commit()

    assert summary.deduped_count == 1
    assert summary.new_email_doc_count == 0
    assert summary.new_attachment_doc_count == 0

    # msg_b should now point at the same Document as msg_a
    await db_session.refresh(msg_b)
    assert msg_b.email_doc_id == parent_doc.doc_id
    assert msg_b.eml_sha256 == real_sha
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: FAIL — `ingest_pending_messages` doesn't exist.

- [ ] **Step 3: Implement `ingest_pending_messages`**

Append to `src/harbor_clerk/mail/ingest.py`:

```python
from sqlalchemy import select

from harbor_clerk.mail.parser import parse_eml
from harbor_clerk.models import WatchedMessage
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import enqueue_stage


async def ingest_pending_messages(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> IngestSummary:
    """For each watched_message in this label with email_doc_id=NULL:
    fetch the .eml, dedup by SHA across labels, create Documents (or link
    to existing), enqueue extract. Caller commits.
    """
    pending = (
        await session.execute(
            select(WatchedMessage).where(
                WatchedMessage.label_id == label.label_id,
                WatchedMessage.email_doc_id.is_(None),
                WatchedMessage.status == "active",
            )
        )
    ).scalars().all()

    fetched_count = 0
    new_email_doc_count = 0
    new_attachment_doc_count = 0
    deduped_count = 0

    for msg in pending:
        try:
            eml_bytes = await fetch_eml_bytes(conn, msg.imap_uid)
        except Exception as exc:
            logger.warning(
                "fetch_eml_bytes failed for label=%s uid=%s: %s",
                label.label_id, msg.imap_uid, exc,
            )
            continue
        fetched_count += 1
        real_sha = hashlib.sha256(eml_bytes).digest()
        msg.eml_sha256 = real_sha

        # Cross-label dedup: any other watched_message already mapped to a Document with this SHA?
        existing = (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.eml_sha256 == real_sha,
                    WatchedMessage.email_doc_id.is_not(None),
                )
            )
        ).scalars().first()
        if existing is not None:
            msg.email_doc_id = existing.email_doc_id
            deduped_count += 1
            continue

        parsed = parse_eml(eml_bytes)
        email_doc = await create_email_document(
            session,
            parsed=parsed,
            eml_bytes=eml_bytes,
            eml_sha256=real_sha,
            label=label,
        )
        await session.flush()
        new_email_doc_count += 1

        attachment_docs = await create_attachment_documents(
            session,
            parsed=parsed,
            parent_doc=email_doc,
            label=label,
        )
        await session.flush()
        new_attachment_doc_count += len(attachment_docs)

        msg.email_doc_id = email_doc.doc_id

        # Enqueue extract for the email and each attachment
        for d in [email_doc, *attachment_docs]:
            enqueue_stage(d.doc_id, JobStage.extract)

    return IngestSummary(
        fetched_count=fetched_count,
        new_email_doc_count=new_email_doc_count,
        new_attachment_doc_count=new_attachment_doc_count,
        deduped_count=deduped_count,
    )
```

Update `src/harbor_clerk/mail/__init__.py` to export the public ingest API:

```python
from harbor_clerk.mail.ingest import IngestSummary, ingest_pending_messages
from harbor_clerk.mail.parser import (
    AttachmentSpec,
    EmailParseResult,
    parse_eml,
    sanitize_subject_for_filename,
)
```

(Add the new names to `__all__`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_ingest.py -v`
Expected: PASS — 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/ingest.py src/harbor_clerk/mail/__init__.py tests/mail/test_ingest.py
git commit -m "feat(mail): ingest_pending_messages — orchestrate fetch+dedup+create+enqueue"
```

---

## Task 9: Document lifecycle — soft-delete on unlabeled

**Files:**
- Create: `src/harbor_clerk/mail/document_lifecycle.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Create: `tests/mail/test_document_lifecycle.py`

When `lifecycle.detect_unlabeled_messages` (Stage 2) marks a `watched_message` as `unlabeled`, Stage 3 needs to soft-delete the associated email Document and its attachment Documents.

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_document_lifecycle.py
"""Lifecycle: watched_message unlabeled → Documents soft-deleted."""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.mail.document_lifecycle import (
    restore_documents_for_relabeled,
    soft_delete_documents_for_unlabeled,
)
from harbor_clerk.models import Document, WatchedMessage
from harbor_clerk.models.enums import PipelineStatus


async def test_soft_delete_marks_email_and_attachments_as_deleted(db_session, watched_label):
    # Set up: 1 email Doc + 2 attachment Docs + a watched_message linking them, all 'active'
    email_doc = Document(
        title="Email",
        canonical_filename="email.eml",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        status="active",
        email_message_id="<unlabel@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    attach1 = Document(
        title="a.pdf", canonical_filename="a.pdf", sha256=b"\x11" * 32,
        pipeline_status=PipelineStatus.ready, mime_type="application/pdf",
        status="active", email_parent_doc_id=email_doc.doc_id,
    )
    attach2 = Document(
        title="b.pdf", canonical_filename="b.pdf", sha256=b"\x22" * 32,
        pipeline_status=PipelineStatus.ready, mime_type="application/pdf",
        status="active", email_parent_doc_id=email_doc.doc_id,
    )
    db_session.add_all([attach1, attach2])
    await db_session.flush()

    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<unlabel@example.com>",
        imap_uid=1,
        eml_sha256=b"\x00" * 32,
        email_doc_id=email_doc.doc_id,
        status="unlabeled",  # already transitioned by Stage 2's lifecycle.detect
        unlabeled_at=datetime.now(UTC),
    )
    db_session.add(msg)
    await db_session.flush()

    deleted = await soft_delete_documents_for_unlabeled(db_session, watched_label)
    await db_session.commit()

    assert deleted == 3  # 1 email + 2 attachments

    await db_session.refresh(email_doc)
    await db_session.refresh(attach1)
    await db_session.refresh(attach2)
    assert email_doc.status == "deleted"
    assert attach1.status == "deleted"
    assert attach2.status == "deleted"


async def test_soft_delete_skips_docs_still_referenced_by_other_labels(db_session, mail_account):
    """If an email is in two labels and only one un-labels, the Documents
    must NOT be deleted — the other label still references them."""
    from harbor_clerk.models import WatchedLabel

    label_a = WatchedLabel(account_id=mail_account.account_id, label_path="A", display_name="A")
    label_b = WatchedLabel(account_id=mail_account.account_id, label_path="B", display_name="B")
    db_session.add_all([label_a, label_b])
    await db_session.flush()

    email_doc = Document(
        title="shared", canonical_filename="shared.eml", sha256=b"\xff" * 32,
        pipeline_status=PipelineStatus.ready, mime_type="message/rfc822",
        status="active", email_message_id="<shared@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    # Both labels reference the same email_doc
    msg_a = WatchedMessage(
        label_id=label_a.label_id, message_id="<shared@example.com>", imap_uid=1,
        eml_sha256=b"\xff" * 32, email_doc_id=email_doc.doc_id, status="unlabeled",
        unlabeled_at=datetime.now(UTC),
    )
    msg_b = WatchedMessage(
        label_id=label_b.label_id, message_id="<shared@example.com>", imap_uid=99,
        eml_sha256=b"\xff" * 32, email_doc_id=email_doc.doc_id, status="active",
    )
    db_session.add_all([msg_a, msg_b])
    await db_session.flush()

    deleted = await soft_delete_documents_for_unlabeled(db_session, label_a)
    await db_session.commit()

    assert deleted == 0  # email_doc still actively referenced by label_b

    await db_session.refresh(email_doc)
    assert email_doc.status == "active"


async def test_restore_documents_for_relabeled(db_session, watched_label):
    """If a previously-unlabeled message comes back, restore its Documents."""
    email_doc = Document(
        title="restored", canonical_filename="restored.eml", sha256=b"\x33" * 32,
        pipeline_status=PipelineStatus.ready, mime_type="message/rfc822",
        status="deleted",  # was soft-deleted earlier
        email_message_id="<restore@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    msg = WatchedMessage(
        label_id=watched_label.label_id, message_id="<restore@example.com>", imap_uid=1,
        eml_sha256=b"\x33" * 32, email_doc_id=email_doc.doc_id,
        status="active",  # came back via re-label
        unlabeled_at=None,
    )
    db_session.add(msg)
    await db_session.flush()

    restored = await restore_documents_for_relabeled(db_session, watched_label)
    await db_session.commit()

    assert restored == 1
    await db_session.refresh(email_doc)
    assert email_doc.status == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_document_lifecycle.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `document_lifecycle.py`**

```python
# src/harbor_clerk/mail/document_lifecycle.py
"""Document soft-delete + restore tied to watched_message status changes.

When Stage 2's lifecycle scan transitions a watched_message to 'unlabeled',
this module marks the associated email Document (and its attachments) as
status='deleted' — but only if no other label still actively references the
same Document. The existing 30-day reaper handles permanent removal.

Restore is the reverse: if a watched_message comes back to 'active', restore
the Documents to status='active'. This handles re-labeling churn.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Document, WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


async def soft_delete_documents_for_unlabeled(
    session: AsyncSession,
    label: WatchedLabel,
) -> int:
    """Soft-delete Documents associated with this label's unlabeled messages.

    Skips Documents that are still actively referenced by another label
    (multi-label messages). Returns the count of Documents transitioned
    to status='deleted'.

    Caller commits.
    """
    unlabeled = (
        await session.execute(
            select(WatchedMessage).where(
                WatchedMessage.label_id == label.label_id,
                WatchedMessage.status == "unlabeled",
                WatchedMessage.email_doc_id.is_not(None),
            )
        )
    ).scalars().all()

    deleted_count = 0
    for msg in unlabeled:
        # Check if ANY active watched_message in another label still points
        # at this Document.
        other_active = (
            await session.execute(
                select(WatchedMessage.message_pk).where(
                    WatchedMessage.email_doc_id == msg.email_doc_id,
                    WatchedMessage.status == "active",
                    WatchedMessage.label_id != label.label_id,
                )
            )
        ).scalars().first()
        if other_active is not None:
            continue  # still referenced; don't delete

        # Soft-delete the email Document
        email_doc = (
            await session.execute(
                select(Document).where(Document.doc_id == msg.email_doc_id)
            )
        ).scalar_one_or_none()
        if email_doc is None:
            continue
        if email_doc.status != "deleted":
            email_doc.status = "deleted"
            deleted_count += 1

        # Soft-delete attachment Documents
        attachments = (
            await session.execute(
                select(Document).where(Document.email_parent_doc_id == email_doc.doc_id)
            )
        ).scalars().all()
        for att in attachments:
            if att.status != "deleted":
                att.status = "deleted"
                deleted_count += 1

    if deleted_count:
        await session.flush()
        logger.info(
            "label %s (%s): soft-deleted %d Documents",
            label.label_id, label.label_path, deleted_count,
        )
    return deleted_count


async def restore_documents_for_relabeled(
    session: AsyncSession,
    label: WatchedLabel,
) -> int:
    """Restore Documents whose watched_message came back to 'active' status.

    Stage 2's sync engine transitions watched_messages from 'unlabeled' to
    'active' when a previously-unlabeled message reappears in the label.
    This function looks for Documents with status='deleted' that now have
    an active watched_message, and restores them.

    Returns the count of Documents transitioned to status='active'.
    Caller commits.
    """
    active_msgs = (
        await session.execute(
            select(WatchedMessage).where(
                WatchedMessage.label_id == label.label_id,
                WatchedMessage.status == "active",
                WatchedMessage.email_doc_id.is_not(None),
            )
        )
    ).scalars().all()

    restored_count = 0
    for msg in active_msgs:
        email_doc = (
            await session.execute(
                select(Document).where(Document.doc_id == msg.email_doc_id)
            )
        ).scalar_one_or_none()
        if email_doc is None or email_doc.status != "deleted":
            continue
        email_doc.status = "active"
        restored_count += 1
        # Restore attachments too
        attachments = (
            await session.execute(
                select(Document).where(
                    Document.email_parent_doc_id == email_doc.doc_id,
                    Document.status == "deleted",
                )
            )
        ).scalars().all()
        for att in attachments:
            att.status = "active"
            restored_count += 1

    if restored_count:
        await session.flush()
        logger.info(
            "label %s (%s): restored %d Documents",
            label.label_id, label.label_path, restored_count,
        )
    return restored_count
```

Add to `src/harbor_clerk/mail/__init__.py`:

```python
from harbor_clerk.mail.document_lifecycle import (
    restore_documents_for_relabeled,
    soft_delete_documents_for_unlabeled,
)
```

(Add to `__all__`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_document_lifecycle.py -v`
Expected: PASS — 3 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/document_lifecycle.py src/harbor_clerk/mail/__init__.py tests/mail/test_document_lifecycle.py
git commit -m "feat(mail): Document lifecycle — soft-delete on unlabeled, restore on re-label"
```

---

## Task 10: MailObserver integration — call ingest + lifecycle in on_tick

**Files:**
- Modify: `src/harbor_clerk/watcher/mail_observer.py`
- Modify: `tests/mail/test_mail_observer.py`

The MailObserver's `on_tick` currently calls sync + lifecycle (Stage 2). This task adds the Stage 3 calls: after sync, run `ingest_pending_messages` to create Documents; after lifecycle, run `soft_delete_documents_for_unlabeled` and `restore_documents_for_relabeled`.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_mail_observer.py`:

```python
async def test_observer_creates_documents_after_sync(db_session, mock_aioimap):
    """End-to-end: sync produces watched_messages, ingest produces Documents."""
    from tests.mail.fixtures.build_eml import build_email_with_attachments
    from harbor_clerk.models import Document

    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="full-flow", provider="gmail",
        imap_host="imap.gmail.com", imap_port=993,
        imap_username="full@example.com",
        app_password_ciphertext=ct, key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id, label_path="Full", display_name="Full",
    )
    db_session.add(label)
    await db_session.commit()

    eml = build_email_with_attachments(
        message_id="<flow1@example.com>",
        subject="End to end",
        body_text="Body.",
        attachments=[("doc.pdf", "application/pdf", b"%PDF fake")],
    )

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1"])  # poll
    mock_aioimap.set_select_response("OK", [
        b"* 1 EXISTS",
        b"* OK [UIDVALIDITY 6666] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    mock_aioimap.set_uid_fetch_response("OK", [
        # First FETCH (sync) returns Message-ID header
        b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <flow1@example.com>\r\n",
        b")",
        b"OK FETCH completed",
        # Second FETCH (ingest) returns full BODY[]
        b"1 (UID 1 BODY[] {%d}" % len(eml),
        eml,
        b")",
        b"OK FETCH completed",
    ])

    observer = MailObserver(poll_interval=0.05)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.4)  # one tick should be enough
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    # Should have one email Document and one attachment Document
    docs = (await db_session.execute(
        select(Document).where(Document.email_message_id == "<flow1@example.com>")
    )).scalars().all()
    assert len(docs) == 2
    titles = sorted(d.title for d in docs)
    assert titles == ["End to end", "doc.pdf"]
```

NOTE: The FakeIMAP currently has `_uid_fetch_response` as a single staged response — but this test needs TWO FETCH responses (one for sync's Message-ID, one for ingest's BODY[]). You may need to extend FakeIMAP to support a queue of FETCH responses. If so, add to `tests/mail/conftest.py` an `_uid_fetch_queue` or change `set_uid_fetch_response` to accept a list and pop sequentially. Adapt the test to use that API.

If extending FakeIMAP is too invasive, an alternative: override the sync response so that ingest's FETCH gets the same response (works because the test only has 1 message). Try this first — if `set_uid_fetch_response` returns the same response for every `uid` call, both sync and ingest get the right data.

Actually re-checking the FakeIMAP from Task 2: each call to `uid("FETCH", ...)` returns whatever the most recent `set_uid_fetch_response` staged. If both sync AND ingest call FETCH, they'll both see the same response. The sync code only parses the Message-ID header line (it uses regex on `Message-I[Dd]:`); the ingest code parses the literal `{nnn}` body. So if you stage the BODY[] FETCH response with the message_id header inline within the eml bytes, BOTH parsers should work off the same response.

Try this version of the FETCH staging:

```python
mock_aioimap.set_uid_fetch_response("OK", [
    b"1 (UID 1 BODY[] {%d}" % len(eml),
    eml,
    b")",
    b"OK FETCH completed",
])
```

The ingest's `_extract_literal` will pull out the `eml` bytes correctly. The sync's `_parse_fetch_response` looks for `Message-ID:` in the lines — and `eml` (built by `build_email_with_attachments`) starts with headers including `Message-ID: <flow1@example.com>`. So even though sync expected `BODY[HEADER.FIELDS (MESSAGE-ID)]` shape, the BODY[] response is a SUPERSET that contains the same data. Try this first.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_mail_observer.py::test_observer_creates_documents_after_sync -v`
Expected: FAIL — observer doesn't call `ingest_pending_messages` yet.

- [ ] **Step 3: Wire ingest + document lifecycle into the MailObserver `on_tick`**

In `src/harbor_clerk/watcher/mail_observer.py`, locate the `on_tick` async function inside `_run_label`. After the existing `detect_unlabeled_messages` call, add ingest and document lifecycle calls. The full updated callback:

```python
        async def on_tick(c: IMAPConnection) -> None:
            async with session_factory() as session:
                lbl = (
                    await session.execute(
                        select(WatchedLabel).where(WatchedLabel.label_id == label_id)
                    )
                ).scalar_one()
                try:
                    if lbl.uidvalidity is None:
                        await sync_label_initial(session, c, lbl)
                    else:
                        try:
                            await sync_label_incremental(session, c, lbl)
                        except UidValidityChanged:
                            await handle_uidvalidity_change(session, c, lbl)
                    await detect_unlabeled_messages(session, c, lbl)
                    # Stage 3: turn newly-discovered watched_messages into Documents
                    await ingest_pending_messages(session, c, lbl)
                    # Stage 3: lifecycle — soft-delete Documents whose watched_messages went unlabeled
                    await soft_delete_documents_for_unlabeled(session, lbl)
                    # Stage 3: lifecycle — restore Documents that came back via re-label
                    await restore_documents_for_relabeled(session, lbl)
                    lbl.last_synced_at = datetime.now(UTC)
                    await session.commit()
                except Exception as exc:
                    logger.exception("sync failed for label %s: %s", label_id, exc)
                    await session.rollback()
```

Add the imports at the top of the file:

```python
from harbor_clerk.mail.document_lifecycle import (
    restore_documents_for_relabeled,
    soft_delete_documents_for_unlabeled,
)
from harbor_clerk.mail.ingest import ingest_pending_messages
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_mail_observer.py -v`
Expected: PASS — all observer tests including the new one green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/mail_observer.py tests/mail/test_mail_observer.py
git commit -m "feat(watcher): MailObserver — invoke ingest + Document lifecycle each tick"
```

---

## Task 11: Verify the full pipeline runs (extract → chunk → embed → ...)

**Files:**
- Modify: `tests/mail/test_ingest.py` (add a smoke test verifying enqueue happened)

This task doesn't add new production code — it adds one test that confirms `enqueue_stage` was called for the email and attachment Documents. The actual extract → chunk → embed → ... pipeline is already exercised by existing tests; we just verify Stage 3 properly hands off.

- [ ] **Step 1: Append the failing test**

Append to `tests/mail/test_ingest.py`:

```python
async def test_ingest_enqueues_extract_for_each_new_doc(
    db_session, watched_label, mock_aioimap, monkeypatch
):
    """ingest_pending_messages should call enqueue_stage(extract) for the
    email Document and each attachment Document."""
    enqueued: list[tuple] = []

    def _capture_enqueue(doc_id, stage, *, priority=0):
        enqueued.append((doc_id, stage, priority))

    monkeypatch.setattr("harbor_clerk.mail.ingest.enqueue_stage", _capture_enqueue)

    eml = build_email_with_attachments(
        message_id="<enq@example.com>", subject="enqueue test",
        attachments=[
            ("a.pdf", "application/pdf", b"%PDF a"),
            ("b.pdf", "application/pdf", b"%PDF b"),
        ],
    )
    msg = WatchedMessage(
        label_id=watched_label.label_id, message_id="<enq@example.com>", imap_uid=7,
        eml_sha256=hashlib.sha256(b"placeholder").digest(),
        status="active", email_doc_id=None,
    )
    db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response("OK", [
        b"7 (UID 7 BODY[] {%d}" % len(eml),
        eml,
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    summary = await ingest_pending_messages(db_session, conn, watched_label)
    await conn.logout()

    assert summary.new_email_doc_count == 1
    assert summary.new_attachment_doc_count == 2

    from harbor_clerk.models.enums import JobStage
    assert len(enqueued) == 3  # email + 2 attachments
    assert all(stage == JobStage.extract for _, stage, _ in enqueued)
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/mail/test_ingest.py::test_ingest_enqueues_extract_for_each_new_doc -v`
Expected: PASS — Task 8's `ingest_pending_messages` already calls `enqueue_stage`.

- [ ] **Step 3: Commit**

```bash
git add tests/mail/test_ingest.py
git commit -m "test(mail): verify ingest enqueues extract for each new Document"
```

---

## Task 12: Test corpus — three .eml fixtures covering common shapes

**Files:**
- Create: `tests/mail/test_corpus.py`

A small test corpus runs `parse_eml` against three crafted .eml shapes that mirror what real Gmail/Outlook produce, ensuring the parser handles them. This is the spec's "curated test corpus" requirement (lighter-weight than the LLM-eval corpus from PR #276 — just parser correctness).

- [ ] **Step 1: Write the test**

```python
# tests/mail/test_corpus.py
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
        maintype="image", subtype="png",
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
        maintype="application", subtype="pdf",
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
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/mail/test_corpus.py -v`
Expected: PASS — all 5 tests green. (If the threaded-reply test fails because `_thread_id_from_references` returns None for multi-element headers, fix the helper to return the first ID even when there are spaces.)

- [ ] **Step 3: Commit**

```bash
git add tests/mail/test_corpus.py
git commit -m "test(mail): corpus — five common .eml shapes for parser coverage"
```

---

## Wrap-up

After Task 12 commits, Stage 3 is complete. Run the full verification suite:

- [ ] **Run all mail tests**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage3 && uv run pytest tests/mail/ -v 2>&1 | tail -10
```

Expected: every mail test green (~50+).

- [ ] **Run the full Python test suite**

```bash
uv run pytest -m "not integration" 2>&1 | tail -5
```

Expected: all green except for the 6 pre-existing `test_rename_originals.py` failures (environmental Postgres GSSAPI issue, unrelated to this work).

- [ ] **Run linting**

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: clean.

- [ ] **Open Stage 3 PR**

```bash
git push -u origin spec/email-stage3
gh pr create --title "feat(email): stage 3 .eml → Document pipeline" \
    --body-file <(cat docs/superpowers/plans/stage3-pr-body.md)
```

The PR description should:
- Link to the spec, plan, and PRs #281 (Stage 1) + #282 (Stage 2)
- Note that this stage is **operator-driven only** — admins with API access (or the existing legacy upload endpoints) can trigger ingestion via Stage 2's API; UI for non-admins is Stage 4
- Stage roadmap (4 → UI: /folders email section, "Add email source" wizard, View-in-Gmail deep link)
