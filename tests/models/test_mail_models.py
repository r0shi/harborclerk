"""Verify the new mail models map correctly and round-trip through the DB."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from harbor_clerk.models import MailAccount, WatchedLabel, WatchedMessage


async def test_mail_account_round_trip(db_session):
    account = MailAccount(
        display_name="Test Gmail",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="alex@example.com",
        app_password_ciphertext=b"\x00" * 100,  # placeholder; cipher comes in Task 4
        key_fingerprint=b"\x00" * 8,
        status="active",
    )
    db_session.add(account)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(MailAccount).where(MailAccount.imap_username == "alex@example.com"))
    ).scalar_one()
    assert fetched.provider == "gmail"
    assert fetched.imap_port == 993
    assert fetched.created_at is not None


async def test_watched_label_cascade_on_account_delete(db_session):
    account = MailAccount(
        display_name="Account to delete",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="cascade@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()

    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Clerk",
        display_name="Clerk",
    )
    db_session.add(label)
    await db_session.flush()
    label_id = label.label_id

    await db_session.delete(account)
    await db_session.flush()

    remaining = (
        await db_session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
    ).scalar_one_or_none()
    assert remaining is None, "watched_labels row should cascade-delete with the account"


async def test_watched_message_unique_per_label(db_session):
    account = MailAccount(
        display_name="Unique-test account",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="unique@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Clerk",
        display_name="Clerk",
    )
    db_session.add(label)
    await db_session.flush()

    msg1 = WatchedMessage(
        label_id=label.label_id,
        message_id="<abc@example.com>",
        imap_uid=1,
        eml_sha256=b"\x11" * 32,
    )
    db_session.add(msg1)
    await db_session.flush()

    msg2 = WatchedMessage(
        label_id=label.label_id,
        message_id="<abc@example.com>",  # same message_id, same label
        imap_uid=2,
        eml_sha256=b"\x22" * 32,
    )
    db_session.add(msg2)
    with pytest.raises(IntegrityError):
        await db_session.flush()
