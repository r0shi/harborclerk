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


async def test_reconcile_respawns_a_label_task_that_died(db_session, mock_aioimap, observer_session_factory):
    """A label task that raised must be respawned, not left dead forever.

    `_tasks` was keyed only on presence, so a task that died from an exception
    stayed in the dict, was never respawned, and never had its exception
    retrieved. One transient error — a connection dropped near Gmail's
    ~29-minute IDLE limit, an `Abort` from the IDLE command — froze that label's
    `last_synced_at` until the watcher process restarted. That is #557's symptom
    one layer out, and it only became reachable once EXAMINE started entering
    the selected state and the IDLE path actually ran.
    """
    import asyncio

    from harbor_clerk.watcher.mail_observer import MailObserver

    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="respawn-test",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="respawn@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(account_id=account.account_id, label_path="Resp", display_name="Resp")
    db_session.add(label)
    await db_session.commit()
    label_id = label.label_id

    observer = MailObserver(session_factory=observer_session_factory)

    # A task that died the way a transient IMAP error would.
    async def _boom():
        raise RuntimeError("connection dropped mid-IDLE")

    dead = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    assert dead.done() and dead.exception() is not None
    observer._tasks[label_id] = dead

    # First reconcile reaps it and arms the backoff (see the backoff test).
    await observer._reconcile(observer_session_factory)
    assert observer._tasks.get(label_id) is None
    assert observer._consecutive_failures[label_id] == 1

    # Once the backoff has elapsed it must actually come back — the point of
    # the fix is that the label recovers without a watcher restart.
    observer._retry_not_before[label_id] = 0.0
    await observer._reconcile(observer_session_factory)

    respawned = observer._tasks.get(label_id)
    assert respawned is not None, "dead task was never respawned — the label would stall forever"
    assert respawned is not dead, "the dead task was left in place"

    await observer.stop()


async def _one_active_label(db_session, name: str):
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name=name,
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username=f"{name}@example.com",
        app_password_ciphertext=ct,
        key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(account_id=account.account_id, label_path=name, display_name=name)
    db_session.add(label)
    await db_session.commit()
    return label.label_id


async def test_respawn_backs_off_instead_of_hammering_the_provider(db_session, mock_aioimap, observer_session_factory):
    """A persistent failure must not respawn every supervisor tick.

    The supervisor ticks every 5s by default. Respawning unconditionally means
    ~720 LOGIN attempts an hour against the provider for a failure that will
    never succeed — a label deleted server-side, DNS down, TLS refused. Each one
    also writes an imap_command_log row. Retrying at all is new; retrying it
    unthrottled would be its own outage.
    """
    import asyncio

    from harbor_clerk.watcher.mail_observer import MailObserver

    label_id = await _one_active_label(db_session, "backoff")
    observer = MailObserver(session_factory=observer_session_factory)

    async def _boom():
        raise RuntimeError("persistent failure")

    # First death: reaped, backoff armed, and NOT immediately respawned.
    dead = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    observer._tasks[label_id] = dead
    observer._spawned_at[label_id] = __import__("time").monotonic()

    await observer._reconcile(observer_session_factory)

    assert label_id not in observer._tasks, "respawned immediately — no backoff was applied"
    assert observer._consecutive_failures[label_id] == 1
    assert observer._retry_not_before[label_id] > 0

    # Backoff must grow, not stay flat.
    first_delay = observer._retry_not_before[label_id] - __import__("time").monotonic()
    observer._consecutive_failures[label_id] = 4
    dead2 = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    observer._tasks[label_id] = dead2
    observer._spawned_at[label_id] = __import__("time").monotonic()
    observer._retry_not_before[label_id] = 0.0  # let it be reaped again

    await observer._reconcile(observer_session_factory)
    second_delay = observer._retry_not_before[label_id] - __import__("time").monotonic()

    assert second_delay > first_delay, f"backoff did not grow: {first_delay:.1f}s then {second_delay:.1f}s"

    await observer.stop()


async def test_reconcile_does_not_spawn_during_shutdown(db_session, mock_aioimap, observer_session_factory):
    """stop() snapshots the task list before awaiting.

    A task created after that snapshot is dropped by the later `_tasks.clear()`
    without ever being cancelled — it keeps an IMAP connection open and keeps
    committing after stop() returned.
    """
    from harbor_clerk.watcher.mail_observer import MailObserver

    await _one_active_label(db_session, "shutdown")
    observer = MailObserver(session_factory=observer_session_factory)
    observer._stop_event.set()

    await observer._reconcile(observer_session_factory)

    assert observer._tasks == {}, "spawned a task during shutdown; it would leak past stop()"


async def test_backoff_never_overflows(db_session, mock_aioimap, observer_session_factory):
    """The failure counter is unbounded; the exponent must not be.

    `5.0 * 2 ** (failures - 1)` raises OverflowError past ~1024. `_reconcile` is
    awaited unguarded in `run()`, so that unwinds the supervisor and every label
    stops syncing until the process restarts — the failure this respawn logic
    exists to prevent, one level up. A label failing fast on every attempt
    reaches it in a few days of ordinary uptime.
    """
    import asyncio

    from harbor_clerk.watcher.mail_observer import MailObserver

    label_id = await _one_active_label(db_session, "overflow")
    observer = MailObserver(session_factory=observer_session_factory)

    async def _boom():
        raise RuntimeError("fails fast, every time")

    for streak in (1_000, 100_000):
        observer._consecutive_failures[label_id] = streak
        dead = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        observer._tasks[label_id] = dead
        observer._spawned_at[label_id] = __import__("time").monotonic()

        await observer._reconcile(observer_session_factory)  # must not raise

        import time as _t

        delay = observer._retry_not_before[label_id] - _t.monotonic()
        assert delay <= 301, f"delay {delay}s exceeded the cap"

    await observer.stop()


async def test_reactivating_a_label_clears_its_backoff(db_session, mock_aioimap, observer_session_factory):
    """A stale gate would silently block recovery right after an operator fix."""
    from harbor_clerk.watcher.mail_observer import MailObserver

    label_id = await _one_active_label(db_session, "reactivate")
    observer = MailObserver(session_factory=observer_session_factory)
    await observer._reconcile(observer_session_factory)

    observer._consecutive_failures[label_id] = 9
    observer._retry_not_before[label_id] = __import__("time").monotonic() + 300

    # Label goes inactive → its task is cancelled and its backoff forgotten.
    from sqlalchemy import select as _select

    from harbor_clerk.models import WatchedLabel as _WL

    lbl = (await db_session.execute(_select(_WL).where(_WL.label_id == label_id))).scalar_one()
    lbl.status = "paused"
    await db_session.commit()

    await observer._reconcile(observer_session_factory)

    assert label_id not in observer._consecutive_failures
    assert label_id not in observer._retry_not_before

    await observer.stop()


async def test_backoff_is_forgotten_for_a_label_that_died_then_went_inactive(
    db_session, mock_aioimap, observer_session_factory
):
    """The cleanup keyed off `actual_ids`, computed *after* the reap loop popped
    dead tasks — so a label that died and *then* went inactive could never
    appear in it, and kept its backoff forever. On reactivation it sat behind a
    stale gate for up to the cap with nothing logged, and its next failure
    escalated from the old streak instead of restarting at the base delay.
    """
    import asyncio

    from sqlalchemy import select as _select

    from harbor_clerk.models import WatchedLabel as _WL
    from harbor_clerk.watcher.mail_observer import MailObserver

    label_id = await _one_active_label(db_session, "forget")
    observer = MailObserver(session_factory=observer_session_factory)

    async def _boom():
        raise RuntimeError("died")

    dead = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    observer._tasks[label_id] = dead
    observer._spawned_at[label_id] = __import__("time").monotonic()

    # Reap while still desired: backoff is armed and the task is gone.
    await observer._reconcile(observer_session_factory)
    assert observer._consecutive_failures.get(label_id) == 1
    assert label_id not in observer._tasks

    # Now the label goes inactive — the state must not survive it.
    lbl = (await db_session.execute(_select(_WL).where(_WL.label_id == label_id))).scalar_one()
    lbl.status = "paused"
    await db_session.commit()

    await observer._reconcile(observer_session_factory)

    assert label_id not in observer._consecutive_failures, "stale backoff survived deactivation"
    assert label_id not in observer._retry_not_before
    assert label_id not in observer._spawned_at

    await observer.stop()


async def test_deliberate_exit_is_not_logged_as_a_failure(db_session, mock_aioimap, observer_session_factory, caplog):
    """`_run_label` returns on purpose when the account goes auth_error or its
    key stops matching. No respawn follows, because the label has left
    desired_ids — so logging "respawning in Ns" promises something that never
    happens, and arming a backoff for it is meaningless.
    """
    import asyncio
    import logging

    from sqlalchemy import select as _select

    from harbor_clerk.models import WatchedLabel as _WL
    from harbor_clerk.watcher.mail_observer import MailObserver

    label_id = await _one_active_label(db_session, "deliberate")
    observer = MailObserver(session_factory=observer_session_factory)

    # Label is already inactive when its task finishes cleanly.
    lbl = (await db_session.execute(_select(_WL).where(_WL.label_id == label_id))).scalar_one()
    lbl.status = "paused"
    await db_session.commit()

    async def _clean_exit():
        return None

    done = asyncio.create_task(_clean_exit())
    await asyncio.sleep(0)
    observer._tasks[label_id] = done
    observer._spawned_at[label_id] = __import__("time").monotonic()

    with caplog.at_level(logging.INFO):
        await observer._reconcile(observer_session_factory)

    text = caplog.text
    assert "respawning" not in text, f"promised a respawn that cannot happen: {text}"
    assert "no longer active" in text
    assert label_id not in observer._consecutive_failures

    await observer.stop()
