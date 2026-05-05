"""UIDVALIDITY change → drop watched_messages, restart with new epoch."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.exceptions import UidValidityChanged
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import (
    handle_uidvalidity_change,
    sync_label_incremental,
)
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_incremental_raises_on_uidvalidity_mismatch(db_session, watched_label, mock_aioimap):
    """Incremental sync detects UIDVALIDITY change and refuses to advance."""
    watched_label.last_uid_seen = 10
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 5 EXISTS",
            b"* OK [UIDVALIDITY 99999] UIDs valid",  # CHANGED
            b"OK SELECT completed",
        ],
    )

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    with pytest.raises(UidValidityChanged):
        await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()


async def test_handle_uidvalidity_change_clears_messages_and_restarts(db_session, watched_label, mock_aioimap):
    """handle_uidvalidity_change deletes old watched_messages and re-runs initial sync."""
    # Pre-populate watched_messages from old epoch
    for uid in [1, 2, 3]:
        msg = WatchedMessage(
            label_id=watched_label.label_id,
            message_id=f"<old-{uid}@example.com>",
            imap_uid=uid,
            eml_sha256=b"\x00" * 32,
            status="active",
        )
        db_session.add(msg)
    watched_label.last_uid_seen = 3
    watched_label.uidvalidity = 12345
    await db_session.flush()

    # Mock IMAP: new epoch with new UIDs
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 2 EXISTS",
            b"* OK [UIDVALIDITY 99999] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1 2"])
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <new1@example.com>\r\n",
            b")",
            b"2 (UID 2 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <new2@example.com>\r\n",
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await handle_uidvalidity_change(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    # Old messages gone, new ones in place
    rows = (
        (
            await db_session.execute(
                select(WatchedMessage)
                .where(WatchedMessage.label_id == watched_label.label_id)
                .order_by(WatchedMessage.imap_uid)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert [r.message_id for r in rows] == ["<new1@example.com>", "<new2@example.com>"]

    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 99999
    assert watched_label.last_uid_seen == 2
    assert summary.new_count == 2
