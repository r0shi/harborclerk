"""API key management endpoints (admin-only)."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyInfo,
    CreateApiKeyRequest,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.db import get_session
from harbor_clerk.models import ApiKey

logger = logging.getLogger(__name__)
router = APIRouter(tags=["api-keys"])


@router.get("/api-keys", response_model=list[ApiKeyInfo])
async def list_api_keys(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at))
    keys = result.scalars().all()
    return [
        ApiKeyInfo(
            key_id=str(k.key_id),
            name=k.name,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    raw_key = generate_api_key()
    key_obj = ApiKey(
        name=body.name,
        key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    session.add(key_obj)
    await log_audit(
        session,
        user_id=admin.id,
        action="create_api_key",
        target_type="api_key",
        target_id=key_obj.key_id,
    )
    await session.commit()
    await session.refresh(key_obj)

    return ApiKeyCreatedResponse(
        key_id=str(key_obj.key_id),
        name=key_obj.name,
        raw_key=raw_key,
        created_at=key_obj.created_at,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ApiKey).where(ApiKey.key_id == key_id))
    key_obj = result.scalar_one_or_none()
    if key_obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    key_obj.is_active = False
    await log_audit(
        session,
        user_id=admin.id,
        action="delete_api_key",
        target_type="api_key",
        target_id=key_id,
    )
    await session.commit()
