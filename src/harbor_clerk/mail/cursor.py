"""Per-label cursor read/write helpers.

The cursor (`uidvalidity`, `last_uid_seen`) lives on the WatchedLabel row.
This module wraps the read/write so the sync engine doesn't need to issue
raw SQL — and centralizes the invariant that within a single uidvalidity
epoch the cursor only advances.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import WatchedLabel


@dataclass(frozen=True)
class LabelCursor:
    """Position within a label's IMAP UID stream."""

    last_uid_seen: int
    uidvalidity: int | None


async def read_cursor(session: AsyncSession, label_id: UUID) -> LabelCursor:
    """Read the cursor from the label row."""
    label = (await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))).scalar_one()
    return LabelCursor(
        last_uid_seen=label.last_uid_seen,
        uidvalidity=label.uidvalidity,
    )


async def write_cursor(
    session: AsyncSession,
    label_id: UUID,
    new: LabelCursor,
) -> None:
    """Write the cursor to the label row.

    Within the same `uidvalidity`, refuses to move `last_uid_seen` backwards.
    A new `uidvalidity` is treated as a fresh epoch — any value is accepted.
    """
    label = (await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))).scalar_one()

    if label.uidvalidity == new.uidvalidity and new.last_uid_seen < label.last_uid_seen:
        raise ValueError(
            f"cursor cannot move backwards within uidvalidity={new.uidvalidity}: "
            f"current={label.last_uid_seen}, attempted={new.last_uid_seen}"
        )

    label.uidvalidity = new.uidvalidity
    label.last_uid_seen = new.last_uid_seen
