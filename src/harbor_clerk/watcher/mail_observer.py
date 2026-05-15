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
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

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
        actual_ids = set(self._tasks.keys())

        # Spawn tasks for newly-active labels
        for lbl in rows:
            if lbl.account.status != "active":
                continue
            if lbl.label_id not in actual_ids:
                self._tasks[lbl.label_id] = asyncio.create_task(self._run_label(lbl.label_id, session_factory))

        # Cancel tasks for labels that are no longer active
        for lid in actual_ids - desired_ids:
            task = self._tasks.pop(lid, None)
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
                acc.last_error = str(exc)
                await session.commit()
            await audit_ctx.__aexit__(None, None, None)
            return
        except Exception as exc:
            logger.warning("connect/login failed for label %s: %s", label_id, exc)
            await audit_ctx.__aexit__(None, None, None)
            return

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
