"""Helper for firing watched_folders_changed NOTIFY from API routes."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

CHANNEL = "watched_folders_changed"


def notify_folder_change(session: Session, folder_id: uuid.UUID, action: str) -> None:
    """Fire NOTIFY watched_folders_changed with payload {folder_id, action}.

    `action` is one of: 'added', 'removed', 'enabled', 'disabled'.
    Caller is responsible for commit (NOTIFY is delivered on commit).
    """
    payload = json.dumps({"folder_id": str(folder_id), "action": action})
    session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})


async def notify_folder_change_async(session: AsyncSession, folder_id: uuid.UUID, action: str) -> None:
    """Async variant of notify_folder_change.

    `action` is one of: 'added', 'removed', 'enabled', 'disabled'.
    Caller is responsible for commit (NOTIFY is delivered on commit). Call this BEFORE
    the commit so the NOTIFY rides on the same transaction.
    """
    payload = json.dumps({"folder_id": str(folder_id), "action": action})
    await session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})
