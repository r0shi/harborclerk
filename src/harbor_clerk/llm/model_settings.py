"""Per-model settings with DB override and global fallback."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.model_settings import ModelSettings

logger = logging.getLogger(__name__)


async def get_model_setting(
    session: AsyncSession,
    model_id: str,
    key: str,
    default: int | str | bool | None = None,
) -> int | str | bool | None:
    """Look up a per-model setting, falling back to default."""
    result = await session.execute(select(ModelSettings.settings).where(ModelSettings.model_id == model_id))
    row = result.scalar_one_or_none()
    if row is not None and key in row:
        return row[key]
    return default
