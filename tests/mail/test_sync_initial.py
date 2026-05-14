"""Initial sync: empty cursor → fetch all UIDs in label → populate
watched_messages rows."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import sync_label_initial
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_initial_sync_empty_label(db_session, watched_label, mock_aioimap):
    """An empty IMAP label produces no watched_messages rows but still
    establishes the cursor (uidvalidity, last_uid_seen=0)."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 0 EXISTS",
            b"* 0 RECENT",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"* OK [UIDNEXT 1] Predicted next UID",
            b"OK [READ-ONLY] SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(
        host="imap.example.com",
        port=993,
        username="x",
        password="y",
    )
    await conn.connect()
    await conn.login()
    summary = await sync_label_initial(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 0
    rows = (
        (await db_session.execute(select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 0

    # Cursor should be set
    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 12345
    assert watched_label.last_uid_seen == 0


async def test_initial_sync_with_three_messages(db_session, watched_label, mock_aioimap):
    """Three messages in the label → three watched_messages rows; cursor
    advances to highest UID."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 3 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1 2 3"])
    # FETCH returns one envelope-stub per message — for initial sync we
    # only need the UID and Message-ID header.
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <msg1@example.com>\r\n",
            b")",
            b"2 (UID 2 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <msg2@example.com>\r\n",
            b")",
            b"3 (UID 3 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <msg3@example.com>\r\n",
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_initial(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 3
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
    assert len(rows) == 3
    assert [r.imap_uid for r in rows] == [1, 2, 3]
    assert [r.message_id for r in rows] == [
        "<msg1@example.com>",
        "<msg2@example.com>",
        "<msg3@example.com>",
    ]
    assert all(r.status == "active" for r in rows)

    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 12345
    assert watched_label.last_uid_seen == 3


async def test_initial_sync_idempotent_on_re_run(db_session, watched_label, mock_aioimap):
    """Running initial sync twice doesn't duplicate rows — UNIQUE
    constraint on (label_id, message_id) plus the eml_sha dedup catches
    repeats."""
    # First run
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 1 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <only@example.com>\r\n",
            b")",
            b"OK FETCH completed",
        ],
    )
    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    await sync_label_initial(db_session, conn, watched_label)
    await db_session.commit()
    await conn.logout()

    # Second run with same data
    conn2 = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn2.connect()
    await conn2.login()
    await sync_label_initial(db_session, conn2, watched_label)
    await db_session.commit()
    await conn2.logout()

    rows = (
        (await db_session.execute(select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_initial_sync_uses_examine_not_select(mock_aioimap, watched_label, db_session, monkeypatch):
    """The initial sync must open the mailbox read-only."""
    from tests.mail.conftest import FakeIMAP

    seen: list[str] = []

    async def _examine(self, mailbox):
        seen.append(("examine", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    async def _select(self, mailbox):
        seen.append(("select", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    monkeypatch.setattr(FakeIMAP, "examine", _examine, raising=False)
    monkeypatch.setattr(FakeIMAP, "select", _select, raising=False)
    FakeIMAP.set_uid_search_response("OK", [b"* SEARCH"])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    await sync_label_initial(db_session, conn, watched_label)

    assert any(call[0] == "examine" for call in seen), f"examine was never called: {seen}"
    assert not any(call[0] == "select" for call in seen), f"select must never be called: {seen}"
