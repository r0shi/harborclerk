"""Lifecycle handling for watched_messages.

When a message is removed from the watched label (un-labeled in Gmail's
UI, or permanently deleted in any provider), our cursor-based incremental
sync doesn't notice — we never see the message disappear because we only
look forward from `last_uid_seen`. This module does the periodic check:
SEARCH ALL UIDs in the label, diff against active watched_messages, mark
any missing ones as 'unlabeled'.

Stage 3 will read these unlabeled rows and soft-delete the associated
email Documents (and their attachment Documents) via the existing 30-day
reaper code path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.models import WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


async def detect_unlabeled_messages(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> int:
    """Diff active watched_messages against the server-side UID set. Mark
    any UIDs in our DB but not on the server as 'unlabeled'.

    Returns the count of rows newly transitioned to 'unlabeled'.

    Caller must have already authenticated. Caller commits.
    """
    # Get current server-side UID set
    select_result, _select_lines = await conn.examine(label.label_path)
    if select_result != "OK":
        logger.warning("EXAMINE %r failed in lifecycle scan", label.label_path)
        return 0
    search_result, search_lines = await conn.uid_search("ALL")
    if search_result != "OK":
        return 0

    server_uids: set[int] = set()
    for line in search_lines:
        for tok in line.split():
            try:
                server_uids.add(int(tok))
            except ValueError:
                continue

    # Get all currently-active watched_messages for this label
    active_rows = (
        (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )

    # Find ones missing from server
    now = datetime.now(UTC)
    transitioned = 0
    for row in active_rows:
        if row.imap_uid not in server_uids:
            row.status = "unlabeled"
            row.unlabeled_at = now
            transitioned += 1

    if transitioned:
        await session.flush()
        logger.info(
            "label %s (%s): transitioned %d watched_messages to 'unlabeled'",
            label.label_id,
            label.label_path,
            transitioned,
        )
    return transitioned
