"""Audit-log model, redaction helper, and reaper tests."""

from __future__ import annotations

import pytest


async def test_imap_command_log_round_trips(db_session, mail_account):
    """Insert a row and read it back."""
    from harbor_clerk.models import ImapCommandLog

    row = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=42,
        duration_ms=17,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.log_id is not None
    assert row.created_at is not None


def test_redact_login_masks_password():
    """LOGIN args contain the user's mail password in cleartext;
    storing them verbatim is a credential leak waiting to happen."""
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args("LOGIN", ("user@example.com", "s3cret-app-password"))
    assert "s3cret-app-password" not in redacted
    assert "user@example.com" in redacted
    assert "[redacted]" in redacted


def test_redact_xoauth2_masks_token():
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args(
        "XOAUTH2",
        ("user@example.com", "ya29.long-bearer-token-value"),
    )
    assert "ya29.long-bearer-token-value" not in redacted
    assert "[redacted]" in redacted


def test_redact_fetch_records_shape_not_body():
    """FETCH responses contain full message bodies. We log the request
    args (which are bounded) but never the response body."""
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args("FETCH", ("1:5", "(BODY.PEEK[])"))
    assert "1:5" in redacted
    assert "BODY.PEEK[]" in redacted


def test_redact_passthrough_for_safe_commands():
    from harbor_clerk.mail.audit import redact_imap_args

    assert redact_imap_args("EXAMINE", ("INBOX",)) == "INBOX"
    assert redact_imap_args("CAPABILITY", ()) == ""


async def test_log_imap_command_writes_row(db_session, mail_account):
    from harbor_clerk.mail.audit import log_imap_command
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    await log_imap_command(
        db_session,
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args=("INBOX",),
        response_status="OK",
        response_bytes=42,
        duration_ms=17,
        error=None,
    )
    await db_session.flush()
    row = (await db_session.execute(select(ImapCommandLog))).scalar_one()
    assert row.command == "EXAMINE"
    assert row.args_redacted == "INBOX"
    assert row.response_status == "OK"
    assert row.error is None


async def test_log_imap_command_redacts_login(db_session, mail_account):
    from harbor_clerk.mail.audit import log_imap_command
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    await log_imap_command(
        db_session,
        account_id=mail_account.account_id,
        label_path=None,
        command="LOGIN",
        args=("user@example.com", "s3cret"),
        response_status="OK",
        response_bytes=0,
        duration_ms=12,
        error=None,
    )
    await db_session.flush()
    row = (await db_session.execute(select(ImapCommandLog))).scalar_one()
    assert "s3cret" not in (row.args_redacted or "")


async def test_reap_old_imap_command_logs(db_session, mail_account):
    """Rows older than retention_days are deleted; younger rows survive."""
    from datetime import UTC, datetime, timedelta

    from harbor_clerk.mail.audit import reap_old_imap_command_logs
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    now = datetime.now(UTC)
    old = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=0,
        duration_ms=0,
        created_at=now - timedelta(days=45),
    )
    young = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=0,
        duration_ms=0,
        created_at=now - timedelta(days=5),
    )
    db_session.add_all([old, young])
    await db_session.flush()

    deleted = await reap_old_imap_command_logs(db_session, retention_days=30)
    await db_session.flush()
    remaining = (await db_session.execute(select(ImapCommandLog))).scalars().all()

    assert deleted == 1
    assert {r.log_id for r in remaining} == {young.log_id}


async def test_examine_records_audit_row(db_session, mail_account, monkeypatch):
    """Calling IMAPConnection.examine() must persist one ImapCommandLog row."""
    from harbor_clerk.mail.imap_client import IMAPConnection
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)

    async def _examine(self, mailbox):
        return "OK", [b"* OK [READ-ONLY]"]

    monkeypatch.setattr(FakeIMAP, "examine", _examine, raising=False)

    conn = IMAPConnection(
        host="h",
        port=993,
        username="u",
        password="p",
        audit_session=db_session,
        account_id=mail_account.account_id,
    )
    await conn.connect()
    await conn.login()
    await conn.examine("INBOX")
    await db_session.flush()

    rows = (await db_session.execute(select(ImapCommandLog).order_by(ImapCommandLog.created_at))).scalars().all()
    assert [r.command for r in rows] == ["LOGIN", "EXAMINE"]
    # login args are redacted; examine args are not
    assert "[redacted]" in (rows[0].args_redacted or "")
    assert rows[1].args_redacted == "INBOX"
