"""Document soft-delete + restore tied to watched_message status changes.

When Stage 2's lifecycle scan transitions a watched_message to 'unlabeled',
this module marks the associated email Document (and its attachments) as
status='deleted' — but only if no other label still actively references the
same Document. The existing 30-day reaper handles permanent removal.

Restore is the reverse: if a watched_message comes back to 'active', restore
the Documents to status='active'. This handles re-labeling churn.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Document, WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


async def soft_delete_documents_for_unlabeled(
    session: AsyncSession,
    label: WatchedLabel,
) -> int:
    """Soft-delete Documents associated with this label's unlabeled messages.

    Skips Documents that are still actively referenced by another label
    (multi-label messages). Returns the count of Documents transitioned
    to status='deleted'.

    Caller commits.
    """
    unlabeled = (
        (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.status == "unlabeled",
                    WatchedMessage.email_doc_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    deleted_count = 0
    for msg in unlabeled:
        # Check if ANY active watched_message in another label still points
        # at this Document.
        other_active = (
            (
                await session.execute(
                    select(WatchedMessage.message_pk).where(
                        WatchedMessage.email_doc_id == msg.email_doc_id,
                        WatchedMessage.status == "active",
                        WatchedMessage.label_id != label.label_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if other_active is not None:
            continue  # still referenced; don't delete

        # Soft-delete the email Document
        email_doc = (
            await session.execute(select(Document).where(Document.doc_id == msg.email_doc_id))
        ).scalar_one_or_none()
        if email_doc is None:
            continue
        if email_doc.status != "deleted":
            email_doc.status = "deleted"
            deleted_count += 1

        # Soft-delete attachment Documents
        attachments = (
            (await session.execute(select(Document).where(Document.email_parent_doc_id == email_doc.doc_id)))
            .scalars()
            .all()
        )
        for att in attachments:
            if att.status != "deleted":
                att.status = "deleted"
                deleted_count += 1

    if deleted_count:
        await session.flush()
        logger.info(
            "label %s (%s): soft-deleted %d Documents",
            label.label_id,
            label.label_path,
            deleted_count,
        )
    return deleted_count


async def restore_documents_for_relabeled(
    session: AsyncSession,
    label: WatchedLabel,
) -> int:
    """Restore Documents whose watched_message came back to 'active' status.

    Stage 2's sync engine transitions watched_messages from 'unlabeled' to
    'active' when a previously-unlabeled message reappears in the label.
    This function looks for Documents with status='deleted' that now have
    an active watched_message, and restores them.

    Returns the count of Documents transitioned to status='active'.
    Caller commits.
    """
    active_msgs = (
        (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.status == "active",
                    WatchedMessage.email_doc_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    restored_count = 0
    for msg in active_msgs:
        email_doc = (
            await session.execute(select(Document).where(Document.doc_id == msg.email_doc_id))
        ).scalar_one_or_none()
        if email_doc is None or email_doc.status != "deleted":
            continue
        email_doc.status = "active"
        restored_count += 1
        # Restore attachments too
        attachments = (
            (
                await session.execute(
                    select(Document).where(
                        Document.email_parent_doc_id == email_doc.doc_id,
                        Document.status == "deleted",
                    )
                )
            )
            .scalars()
            .all()
        )
        for att in attachments:
            att.status = "active"
            restored_count += 1

    if restored_count:
        await session.flush()
        logger.info(
            "label %s (%s): restored %d Documents",
            label.label_id,
            label.label_path,
            restored_count,
        )
    return restored_count
