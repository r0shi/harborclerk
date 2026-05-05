"""Incremental sync: cursor advances on new messages, ignores already-seen ones."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import sync_label_incremental
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_incremental_sync_fetches_only_new_uids(db_session, watched_label, mock_aioimap):
    # Set existing cursor: we've seen up to UID 5
    watched_label.last_uid_seen = 5
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 7 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    # Server now has UIDs 1-7; the search query `UID 6:*` returns only 6 and 7.
    mock_aioimap.set_uid_search_response("OK", [b"6 7"])
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"6 (UID 6 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <new1@example.com>\r\n",
            b")",
            b"7 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <new2@example.com>\r\n",
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 2
    assert summary.new_count == 2

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
    assert [r.imap_uid for r in rows] == [6, 7]

    await db_session.refresh(watched_label)
    assert watched_label.last_uid_seen == 7


async def test_incremental_sync_no_new_messages(db_session, watched_label, mock_aioimap):
    """If `UID last+1:*` returns empty, sync is a no-op."""
    watched_label.last_uid_seen = 100
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 5 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()

    assert summary.fetched_count == 0
    assert summary.new_count == 0

    await db_session.refresh(watched_label)
    assert watched_label.last_uid_seen == 100  # unchanged
