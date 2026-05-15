"""Lifecycle: detect messages that left the label, mark as 'unlabeled'."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.lifecycle import detect_unlabeled_messages
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_detect_unlabeled_marks_missing_messages(db_session, watched_label, mock_aioimap):
    """Three messages were active; server now reports only two — mark
    the missing one as 'unlabeled'."""
    for uid in [1, 2, 3]:
        msg = WatchedMessage(
            label_id=watched_label.label_id,
            message_id=f"<msg{uid}@example.com>",
            imap_uid=uid,
            eml_sha256=b"\x00" * 32,
            status="active",
        )
        db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 2 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1 3"])  # UID 2 is gone

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    unlabeled_count = await detect_unlabeled_messages(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert unlabeled_count == 1

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
    statuses = {r.imap_uid: r.status for r in rows}
    unlabeled_at = {r.imap_uid: r.unlabeled_at for r in rows}
    assert statuses == {1: "active", 2: "unlabeled", 3: "active"}
    assert unlabeled_at[2] is not None
    assert unlabeled_at[1] is None
    assert unlabeled_at[3] is None


async def test_detect_unlabeled_idempotent(db_session, watched_label, mock_aioimap):
    """Running twice doesn't re-trigger unlabeled_at on already-unlabeled rows."""
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<gone@example.com>",
        imap_uid=1,
        eml_sha256=b"\x00" * 32,
        status="unlabeled",
        unlabeled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 0 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    unlabeled_count = await detect_unlabeled_messages(db_session, conn, watched_label)
    await conn.logout()

    assert unlabeled_count == 0  # nothing newly unlabeled
    refetched = (await db_session.execute(select(WatchedMessage))).scalars().first()
    assert refetched.unlabeled_at == datetime(2026, 1, 1, tzinfo=UTC)  # unchanged


async def test_detect_unlabeled_uses_examine_not_select(mock_aioimap, watched_label, db_session, monkeypatch):
    """The lifecycle delete-detection must open the mailbox read-only via EXAMINE, never SELECT."""
    from tests.mail.conftest import FakeIMAP

    seen: list[tuple[str, str]] = []

    async def _examine(self, mailbox):
        seen.append(("examine", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    async def _select(self, mailbox):
        seen.append(("select", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    monkeypatch.setattr(FakeIMAP, "examine", _examine, raising=False)
    monkeypatch.setattr(FakeIMAP, "select", _select, raising=False)
    FakeIMAP.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    await detect_unlabeled_messages(db_session, conn, watched_label)

    assert any(call[0] == "examine" for call in seen), f"examine was never called: {seen}"
    assert not any(call[0] == "select" for call in seen), f"select must never be called: {seen}"
