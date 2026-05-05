"""Cursor read/write helpers for per-label sync state."""

from uuid import uuid4

import pytest

from harbor_clerk.mail.cursor import LabelCursor, read_cursor, write_cursor
from harbor_clerk.models import MailAccount, WatchedLabel


@pytest.fixture
async def label(db_session) -> WatchedLabel:
    account = MailAccount(
        display_name="cursor-test",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username=f"cursor-{uuid4()}@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()
    lbl = WatchedLabel(
        account_id=account.account_id,
        label_path="Cursor",
        display_name="Cursor",
    )
    db_session.add(lbl)
    await db_session.flush()
    return lbl


async def test_read_cursor_empty(db_session, label):
    """A freshly-created label has cursor (last_uid_seen=0, uidvalidity=None)."""
    cursor = await read_cursor(db_session, label.label_id)
    assert cursor.last_uid_seen == 0
    assert cursor.uidvalidity is None


async def test_write_cursor_updates_label_row(db_session, label):
    new = LabelCursor(last_uid_seen=42, uidvalidity=12345)
    await write_cursor(db_session, label.label_id, new)
    await db_session.commit()

    refetched = await read_cursor(db_session, label.label_id)
    assert refetched.last_uid_seen == 42
    assert refetched.uidvalidity == 12345


async def test_write_cursor_advances_only(db_session, label):
    """write_cursor never moves last_uid_seen backwards within the same uidvalidity."""
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=100, uidvalidity=12345))
    await db_session.commit()

    # Try to move backwards
    with pytest.raises(ValueError, match="cursor cannot move backwards"):
        await write_cursor(
            db_session,
            label.label_id,
            LabelCursor(last_uid_seen=50, uidvalidity=12345),
        )


async def test_write_cursor_resets_on_uidvalidity_change(db_session, label):
    """A different uidvalidity is a new epoch — last_uid_seen restarts."""
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=100, uidvalidity=12345))
    await db_session.commit()

    # New uidvalidity → cursor resets
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=5, uidvalidity=99999))
    await db_session.commit()

    refetched = await read_cursor(db_session, label.label_id)
    assert refetched.uidvalidity == 99999
    assert refetched.last_uid_seen == 5
