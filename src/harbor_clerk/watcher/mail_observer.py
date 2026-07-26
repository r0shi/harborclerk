"""Mail observer: orchestrates per-label sync tasks under the watcher daemon.

Architecture:
  - One MailObserver per process.
  - On each supervisor tick (every `supervisor_interval` seconds):
      - Query DB for `watched_labels` where status='active' AND
        account.status='active'.
      - For each label not yet watched: spawn a sync task.
      - For each watched label that's no longer in the active set: cancel
        its task.
  - Per-label sync task: open IMAP connection, run initial-or-incremental
    sync, enter poll_or_idle_loop with on_tick = (incremental sync +
    lifecycle scan). On AuthError: mark account, exit. On UidValidityChanged:
    rescan, continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from harbor_clerk.error_text import describe_error, message_or_type
from harbor_clerk.mail.audit import audit_session_scope
from harbor_clerk.mail.document_lifecycle import (
    restore_documents_for_relabeled,
    soft_delete_documents_for_unlabeled,
)
from harbor_clerk.mail.exceptions import AuthError, UidValidityChanged
from harbor_clerk.mail.idle import poll_or_idle_loop
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.ingest import ingest_pending_messages
from harbor_clerk.mail.lifecycle import detect_unlabeled_messages
from harbor_clerk.mail.sync import (
    handle_uidvalidity_change,
    sync_label_incremental,
    sync_label_initial,
)
from harbor_clerk.models import MailAccount, WatchedLabel
from harbor_clerk.secrets import get_cipher

logger = logging.getLogger(__name__)


def _default_session_factory() -> async_sessionmaker:
    """Build the production session factory (lazy import avoids module-load side-effects)."""
    from harbor_clerk.db import engine

    return async_sessionmaker(engine, expire_on_commit=False)


# Respawn backoff. 5s base matches the supervisor tick, so a one-off blip still
# recovers on the next pass; 5 minutes caps a persistent failure at ~12 login
# attempts an hour rather than ~720.
_BASE_RESPAWN_BACKOFF = 5.0
_MAX_RESPAWN_BACKOFF = 300.0
# Clamp the *exponent*, not just the resulting delay. `2 ** (failures - 1)` is an
# int, and 5.0 * 2**1024 raises OverflowError — which unwinds _reconcile and
# run(), so watcher/main.py logs "mail observer exited" and every label stops
# syncing until the process restarts. At the 300s cap a label failing in under
# _HEALTHY_RUN_SECONDS on every attempt reaches that in ~3.5 days, which is
# ordinary uptime for an always-on appliance. That is the failure this respawn
# logic exists to prevent, reintroduced one level up.
_MAX_BACKOFF_EXPONENT = 8
# A task that stayed up this long was working; its death is a new incident, not
# a continuation of an earlier streak.
_HEALTHY_RUN_SECONDS = 120.0


class MailObserver:
    def __init__(
        self,
        *,
        supervisor_interval: float = 5.0,
        poll_interval: float = 120.0,
        session_factory: async_sessionmaker | None = None,
    ):
        self.supervisor_interval = supervisor_interval
        self.poll_interval = poll_interval
        self._session_factory = session_factory
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()
        # Respawn bookkeeping. Without backoff, a *persistent* failure — DNS
        # down, a label deleted server-side, TLS refused — would respawn every
        # supervisor_interval, i.e. ~720 LOGIN attempts an hour against the
        # provider, each writing an imap_command_log row. Retrying at all is
        # new behaviour; retrying it unthrottled would be its own outage.
        self._consecutive_failures: dict[UUID, int] = {}
        self._retry_not_before: dict[UUID, float] = {}
        self._spawned_at: dict[UUID, float] = {}

    async def stop(self) -> None:
        """Signal the supervisor and all per-label tasks to stop."""
        self._stop_event.set()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def run(self) -> None:
        """Supervisor loop. Returns when stop() is called."""
        session_factory = self._session_factory or _default_session_factory()
        try:
            while not self._stop_event.is_set():
                await self._reconcile(session_factory)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.supervisor_interval)
                    break  # stop_event was set
                except TimeoutError:
                    continue  # tick again
        finally:
            await self.stop()

    def _forget(self, label_id: UUID) -> None:
        """Drop every scrap of per-label state. One place, so a new dict cannot
        be added to __init__ and quietly outlive the label it describes."""
        self._consecutive_failures.pop(label_id, None)
        self._retry_not_before.pop(label_id, None)
        self._spawned_at.pop(label_id, None)

    async def _reconcile(self, session_factory: async_sessionmaker) -> None:
        """Spawn / cancel per-label tasks to match the desired-state DB query."""
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WatchedLabel)
                        .options(selectinload(WatchedLabel.account))
                        .where(WatchedLabel.status == "active")
                    )
                )
                .scalars()
                .all()
            )

        desired_ids = {lbl.label_id for lbl in rows if lbl.account.status == "active"}

        # Reap finished tasks before deciding what is running. Without this, a
        # task that died from an exception stays in `_tasks` forever, so it is
        # never respawned and its exception is never retrieved — one transient
        # error (a dropped connection near Gmail's ~29-minute IDLE limit, an
        # `Abort` from the IDLE command) freezes that label's `last_synced_at`
        # until the watcher process restarts. That is #557's symptom exactly,
        # one layer out, and it only became reachable once EXAMINE started
        # entering the selected state and the IDLE path went live.
        now = time.monotonic()
        for lid, task in list(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(lid, None)
            ran_for = now - self._spawned_at.pop(lid, now)

            # `_run_label` swallows CancelledError, so a cancelled task finishes
            # with a result rather than as cancelled — `task.cancelled()` alone
            # would classify every shutdown as a clean exit and respawn it.
            if task.cancelled() or self._stop_event.is_set():
                self._forget(lid)
                continue

            exc = task.exception()

            # `_run_label` also *returns* on purpose when the account has gone
            # auth_error or its key no longer matches. Those are not failures
            # and no respawn is coming, because the label has left desired_ids
            # — so arming a backoff and logging "respawning in Ns" promises
            # something that will never happen.
            if lid not in desired_ids:
                self._forget(lid)
                logger.info(
                    "label %s sync task stopped after %.0fs; label or account no longer active",
                    lid,
                    ran_for,
                )
                continue
            # A task that ran a good while got as far as working; treat its
            # death as a fresh incident rather than escalating an old streak.
            failures = 1 if ran_for >= _HEALTHY_RUN_SECONDS else self._consecutive_failures.get(lid, 0) + 1
            self._consecutive_failures[lid] = failures
            delay = min(_MAX_RESPAWN_BACKOFF, _BASE_RESPAWN_BACKOFF * (2 ** min(failures - 1, _MAX_BACKOFF_EXPONENT)))
            self._retry_not_before[lid] = now + delay

            if exc is not None:
                logger.warning(
                    "label %s sync task died after %.0fs (failure %d); respawning in %.0fs: %s",
                    lid,
                    ran_for,
                    failures,
                    delay,
                    describe_error(exc),
                )
            else:
                # Still desired, but the coroutine returned rather than raised:
                # the connect/login failure path does exactly this, having
                # already logged and latched the reason. "without error" would
                # be wrong — it failed, it just did not propagate.
                logger.warning(
                    "label %s sync task returned early after %.0fs (failure %d); respawning in %.0fs",
                    lid,
                    ran_for,
                    failures,
                    delay,
                )

        actual_ids = set(self._tasks.keys())

        # Spawn tasks for newly-active labels (and anything reaped whose backoff
        # has elapsed). Never spawn during shutdown: stop() snapshots the task
        # list before awaiting, so a task created after that snapshot would be
        # dropped by the later _tasks.clear() without ever being cancelled —
        # leaving an IMAP connection open and still committing after stop().
        if self._stop_event.is_set():
            return
        for lbl in rows:
            if lbl.account.status != "active":
                continue
            if lbl.label_id in actual_ids:
                continue
            if now < self._retry_not_before.get(lbl.label_id, 0.0):
                continue
            self._spawned_at[lbl.label_id] = now
            self._tasks[lbl.label_id] = asyncio.create_task(self._run_label(lbl.label_id, session_factory))

        # Cancel tasks for labels that are no longer active, and forget the
        # bookkeeping for anything undesired — keyed off every id we hold state
        # for, not just running tasks. `actual_ids` is computed *after* the reap
        # loop popped dead tasks, so a label that died and then went inactive
        # would never appear here and would keep its backoff forever: on
        # reactivation it sits behind a stale gate for up to the cap, silently,
        # and its next failure escalates from the old streak instead of
        # restarting at the base delay.
        known = set(self._tasks) | set(self._consecutive_failures) | set(self._retry_not_before) | set(self._spawned_at)
        for lid in known - desired_ids:
            task = self._tasks.pop(lid, None)
            self._forget(lid)
            if task is not None:
                task.cancel()

    async def _run_label(self, label_id: UUID, session_factory: async_sessionmaker) -> None:
        """Per-label sync task. Opens an IMAP connection, runs sync until cancelled or auth-fails."""
        async with session_factory() as session:
            label = (
                await session.execute(
                    select(WatchedLabel)
                    .options(selectinload(WatchedLabel.account))
                    .where(WatchedLabel.label_id == label_id)
                )
            ).scalar_one()
            account = label.account

            cipher = get_cipher()
            try:
                password = cipher.decrypt(account.app_password_ciphertext, account.key_fingerprint).decode()
            except Exception as exc:
                logger.warning("decrypt failed for account %s: %s", account.account_id, exc)
                account.status = "key_mismatch"
                account.last_error = "key fingerprint does not match active master key"
                await session.commit()
                return

            # Audit session outlives each per-tick sync session so that IMAP
            # command records are committed independently — if a sync tick rolls
            # back, the audit trail of what was attempted is preserved.
            audit_ctx = audit_session_scope()
            audit_session = await audit_ctx.__aenter__()

            conn = IMAPConnection(
                host=account.imap_host,
                port=account.imap_port,
                username=account.imap_username,
                password=password,
                audit_session=audit_session,
                account_id=account.account_id,
            )

        try:
            await conn.connect()
            await conn.login()
        except AuthError as exc:
            async with session_factory() as session:
                acc = (
                    await session.execute(select(MailAccount).where(MailAccount.account_id == account.account_id))
                ).scalar_one()
                acc.status = "auth_error"
                acc.last_error = message_or_type(exc)
                await session.commit()
            await audit_ctx.__aexit__(None, None, None)
            return
        except Exception as exc:
            # Latch it. Without this the account stays `active` with no
            # last_error, so the UI shows a healthy account that never syncs —
            # and the supervisor retries forever with nothing explaining why.
            logger.warning("connect/login failed for label %s: %s", label_id, describe_error(exc))
            try:
                async with session_factory() as session:
                    acc = (
                        await session.execute(select(MailAccount).where(MailAccount.account_id == account.account_id))
                    ).scalar_one_or_none()
                    if acc is not None:
                        acc.last_error = describe_error(exc)
                        await session.commit()
            except Exception:
                # "connect failed" and "DB down" co-occur constantly. Letting
                # this escape would skip the audit exit below and strand its
                # session + pooled connection — once per retry, now that we
                # retry.
                logger.exception("could not record connect failure for label %s", label_id)
            finally:
                with contextlib.suppress(Exception):
                    await conn.logout()
                await audit_ctx.__aexit__(None, None, None)
            return

        # Login worked: clear any latched failure, or the UI shows a broken
        # account that is in fact syncing — the inverse of the bug this fixes.
        try:
            async with session_factory() as session:
                acc = (
                    await session.execute(select(MailAccount).where(MailAccount.account_id == account.account_id))
                ).scalar_one_or_none()
                if acc is not None and acc.last_error is not None:
                    acc.last_error = None
                    acc.last_connected_at = datetime.now(UTC)
                    await session.commit()
        except Exception:
            logger.exception("could not clear last_error for label %s", label_id)

        async def on_tick(c: IMAPConnection) -> None:
            async with session_factory() as session:
                lbl = (
                    await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
                ).scalar_one()
                try:
                    if lbl.uidvalidity is None:
                        await sync_label_initial(session, c, lbl)
                    else:
                        try:
                            await sync_label_incremental(session, c, lbl)
                        except UidValidityChanged:
                            await handle_uidvalidity_change(session, c, lbl)
                    await detect_unlabeled_messages(session, c, lbl)
                    # Stage 3: turn newly-discovered watched_messages into Documents
                    await ingest_pending_messages(session, c, lbl)
                    # Stage 3: lifecycle — soft-delete Documents whose watched_messages went unlabeled
                    await soft_delete_documents_for_unlabeled(session, lbl)
                    # Stage 3: lifecycle — restore Documents that came back via re-label
                    await restore_documents_for_relabeled(session, lbl)
                    lbl.last_synced_at = datetime.now(UTC)
                    await session.commit()
                except Exception as exc:
                    logger.exception("sync failed for label %s: %s", label_id, exc)
                    await session.rollback()

        try:
            # Initial tick (in case label has no cursor yet)
            await on_tick(conn)
            await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=self.poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            await conn.logout()
            await audit_ctx.__aexit__(None, None, None)
