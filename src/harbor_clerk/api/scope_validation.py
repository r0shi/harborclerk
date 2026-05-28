"""Validation: ensure ScopeSpec.folder_ids references existing, available folders."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.schemas.scope import ScopeSpec
from harbor_clerk.models.watched import WatchedFolder


async def validate_scope_folders(scope: ScopeSpec | None, session: AsyncSession) -> None:
    """Raise 422 if any folder_id is unknown or unavailable.

    No-op when scope is None or scope.folder_ids is None/empty.
    """
    if scope is None or not scope.folder_ids:
        return

    folder_ids: list[uuid.UUID] = list(scope.folder_ids)
    rows = (
        await session.execute(
            select(WatchedFolder.folder_id, WatchedFolder.unavailable_reason).where(
                WatchedFolder.folder_id.in_(folder_ids)
            )
        )
    ).all()

    found_ids = {r[0] for r in rows}
    unknown = [str(fid) for fid in folder_ids if fid not in found_ids]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown folder_ids: {unknown}")

    unavailable = [str(r[0]) for r in rows if r[1] is not None]
    if unavailable:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot scope to unavailable folders: {unavailable}",
        )
