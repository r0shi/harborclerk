"""Mail observer: launches per-label sync tasks, supervises restarts."""

import asyncio
import base64
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harbor_clerk.models import MailAccount, WatchedLabel, WatchedMessage
from harbor_clerk.secrets import get_cipher
from harbor_clerk.watcher.mail_observer import MailObserver


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    from harbor_clerk.secrets import _accessor

    _accessor.reset()
    yield
    _accessor.reset()


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
    return FakeIMAP


@pytest.fixture
def observer_session_factory(_engine):
    """Session factory backed by the test engine (NullPool — no cross-loop leakage)."""
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def test_observer_runs_initial_sync_for_active_label(
    db_session, mock_aioimap, observer_session_factory, monkeypatch
):
    """Observer started against a label with no cursor → runs initial sync."""
    # Patch enqueue_stage so it doesn't try to open a sync DB session in test context
    monkeypatch.setattr("harbor_clerk.mail.ingest.enqueue_stage", lambda *a, **kw: None)

    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="obs-test",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="obs@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Obs",
        display_name="Obs",
    )
    db_session.add(label)
    await db_session.commit()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1"])  # no IDLE → polling
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 1 EXISTS",
            b"* OK [UIDVALIDITY 5555] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
            b"Message-ID: <obs1@example.com>\r\n",
            b")",
            b"OK FETCH completed",
        ],
    )

    observer = MailObserver(poll_interval=0.05, session_factory=observer_session_factory)

    # Run for ~0.3s — long enough for one initial-sync tick to complete
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.3)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    rows = (
        (await db_session.execute(select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].message_id == "<obs1@example.com>"


async def test_observer_skips_paused_labels(db_session, mock_aioimap, observer_session_factory):
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="paused-test",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="paused@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Paused",
        display_name="Paused",
        status="paused",
    )
    db_session.add(label)
    await db_session.commit()

    mock_aioimap.set_login_response("OK", b"OK")  # never called

    observer = MailObserver(poll_interval=0.05, session_factory=observer_session_factory)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.2)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    # No watched_messages should have been created
    rows = (
        (await db_session.execute(select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_observer_skips_auth_error_accounts(db_session, mock_aioimap, observer_session_factory):
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="auth-err-test",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="autherr@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
        status="auth_error",
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="WontSync",
        display_name="WontSync",
    )
    db_session.add(label)
    await db_session.commit()

    observer = MailObserver(poll_interval=0.05, session_factory=observer_session_factory)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.2)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    rows = (
        (await db_session.execute(select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_observer_creates_documents_after_sync(db_session, mock_aioimap, observer_session_factory, monkeypatch):
    """End-to-end: sync produces watched_messages, ingest produces Documents."""
    from harbor_clerk.models import Document
    from tests.mail.fixtures.build_eml import build_email_with_attachments

    # Patch enqueue_stage so it doesn't try to open a sync DB session in test context
    monkeypatch.setattr("harbor_clerk.mail.ingest.enqueue_stage", lambda *a, **kw: None)

    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="full-flow",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="full@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Full",
        display_name="Full",
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
    mock_aioimap.set_select_response(
        "OK",
        [
            b"* 1 EXISTS",
            b"* OK [UIDVALIDITY 6666] UIDs valid",
            b"OK SELECT completed",
        ],
    )
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    # Stage the FETCH response as the full BODY[] — both sync's Message-ID parser
    # and ingest's literal extractor work off this same staged data.
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    observer = MailObserver(poll_interval=0.05, session_factory=observer_session_factory)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.4)  # one tick should be enough
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    # Should have one email Document and one attachment Document
    docs = (
        (await db_session.execute(select(Document).where(Document.email_message_id == "<flow1@example.com>")))
        .scalars()
        .all()
    )
    assert len(docs) == 2
    titles = sorted(d.title for d in docs)
    assert titles == ["End to end", "doc.pdf"]
