"""Audit log helper."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.audit_log import AuditLog


async def log_audit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    entry = AuditLog(
        user_id=user_id,
        api_key_id=api_key_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
    )
    session.add(entry)
