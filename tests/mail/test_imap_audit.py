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
