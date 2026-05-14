"""Incremental sync: cursor advances on new messages, ignores already-seen ones."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import sync_label_incremental
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
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


async def test_incremental_sync_restores_unlabeled_messages_on_relabel(db_session, watched_label, mock_aioimap):
    """Regression: when a message is removed from the label (status='unlabeled')
    and then re-applied, the sync's dedup branch must flip it back to 'active'
    so Stage 3's restore_documents_for_relabeled finds it.

    Pre-fix: the dedup branch unconditionally `continue`'d on an existing row,
    leaving status='unlabeled' indefinitely. restore_documents_for_relabeled
    found 0 rows and the previously-soft-deleted Documents stayed deleted
    forever.
    """
    import hashlib
    from datetime import UTC, datetime

    # Seed: one existing watched_message at status='unlabeled' (as if Stage 2's
    # lifecycle had detected it disappeared from the label earlier).
    watched_label.last_uid_seen = 9
    watched_label.uidvalidity = 12345
    await db_session.flush()
    existing = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<relabel@example.com>",
        imap_uid=10,
        eml_sha256=hashlib.sha256(b"placeholder").digest(),
        status="unlabeled",
        unlabeled_at=datetime.now(UTC),
    )
    db_session.add(existing)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 1 EXISTS",
            b"* OK [UIDVALIDITY 12345] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    # Server reports the same UID 10 (label was re-applied — UID may even
    # differ in practice, but the message_id match is what dedup pivots on).
    mock_aioimap.set_uid_search_response("OK", [b"10"])
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"10 (UID 10 BODY[HEADER.FIELDS (MESSAGE-ID)] {44}",
            b"Message-ID: <relabel@example.com>\r\n",
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

    # Dedup branch hit (same message_id), and the row was flipped.
    assert summary.new_count == 0
    assert summary.duplicate_count == 1
    await db_session.refresh(existing)
    assert existing.status == "active"
    assert existing.unlabeled_at is None


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


async def test_incremental_sync_uses_examine_not_select(mock_aioimap, watched_label, db_session, monkeypatch):
    """The incremental sync must open the mailbox read-only via EXAMINE, never SELECT."""
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
    FakeIMAP.set_uid_search_response("OK", [b"* SEARCH"])

    # Set up a prior cursor so the incremental path is exercised.
    watched_label.last_uid_seen = 5
    watched_label.uidvalidity = 12345
    await db_session.flush()

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    await sync_label_incremental(db_session, conn, watched_label)

    assert any(call[0] == "examine" for call in seen), f"examine was never called: {seen}"
    assert not any(call[0] == "select" for call in seen), f"select must never be called: {seen}"
