"""API key management endpoints (admin-only)."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyOut,
    CreateApiKeyRequest,
    PatchApiKeyRequest,
    ScopePreviewRequest,
    ScopePreviewResponse,
)
from harbor_clerk.api.scope import KeyScope, apply_key_scope
from harbor_clerk.audit import log_audit
from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.db import get_session
from harbor_clerk.models import ApiKey
from harbor_clerk.models.document import Document

logger = logging.getLogger(__name__)
router = APIRouter(tags=["api-keys"])


def _scope_summary(api_key: ApiKey) -> str:
    parts = []
    if api_key.permission_tier != "full":
        parts.append(api_key.permission_tier.capitalize() + " only")
    if api_key.scope_topic_ids:  # None and [] both mean "no restriction"
        n = len(api_key.scope_topic_ids)
        parts.append(f"{n} topic{'s' if n != 1 else ''}")
    if api_key.scope_folder_ids:
        n = len(api_key.scope_folder_ids)
        parts.append(f"{n} folder{'s' if n != 1 else ''}")
    if api_key.max_snippet_chars:
        parts.append(f"max {api_key.max_snippet_chars} chars")
    if api_key.expires_at:
        parts.append(f"expires {api_key.expires_at.date().isoformat()}")
    return ", ".join(parts) if parts else "Full access"


def _api_key_to_out(api_key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        key_id=str(api_key.key_id),
        name=api_key.name,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        max_snippet_chars=api_key.max_snippet_chars,
        scope_summary=_scope_summary(api_key),
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.scalars().all()
    return [_api_key_to_out(k) for k in keys]


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    raw_key = generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_hash=hash_api_key(raw_key),
        expires_at=body.expires_at,
        permission_tier=body.permission_tier,
        tool_overrides=body.tool_overrides or {},
        scope_topic_ids=body.scope_topic_ids,
        scope_folder_ids=body.scope_folder_ids,
        max_snippet_chars=body.max_snippet_chars,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    await log_audit(
        session,
        user_id=admin.id,
        action="create_api_key",
        target_type="api_key",
        target_id=api_key.key_id,
    )
    await session.commit()
    return ApiKeyCreatedResponse(
        key_id=str(api_key.key_id),
        name=api_key.name,
        raw_key=raw_key,
        mcp_path=f"/t/{raw_key}",
        created_at=api_key.created_at,
    )


@router.patch("/api-keys/{key_id}", response_model=ApiKeyOut)
async def patch_api_key(
    key_id: uuid.UUID,
    body: PatchApiKeyRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    # exclude_unset so null vs absent is distinguishable
    patch = body.model_dump(exclude_unset=True)
    # Non-nullable DB columns can't be set to None via PATCH
    _non_nullable = {"name", "permission_tier", "tool_overrides"}
    for field in _non_nullable:
        if field in patch and patch[field] is None:
            raise HTTPException(
                status_code=422,
                detail=f"Field '{field}' cannot be null",
            )
    for field, value in patch.items():
        setattr(api_key, field, value)
    await log_audit(
        session,
        user_id=admin.id,
        action="patch_api_key",
        target_type="api_key",
        target_id=key_id,
        detail={"fields": list(patch.keys())},
    )
    await session.commit()
    await session.refresh(api_key)
    return _api_key_to_out(api_key)


@router.get("/api-keys/{key_id}/scope-preview", response_model=ScopePreviewResponse)
async def scope_preview(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    total = (await session.execute(select(func.count(Document.doc_id)).where(Document.status == "active"))).scalar_one()

    scope = KeyScope(
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        max_snippet_chars=api_key.max_snippet_chars,
    )
    fake_principal = Principal(type="api_key", id=key_id, role="user", key_scope=scope)
    visible_query = apply_key_scope(
        select(func.count(Document.doc_id)).where(Document.status == "active"),
        fake_principal,
    )
    visible = (await session.execute(visible_query)).scalar_one()

    return ScopePreviewResponse(
        accessible_documents=visible,
        total_documents=total,
    )


@router.post("/api-keys/scope-preview", response_model=ScopePreviewResponse)
async def scope_preview_adhoc(
    body: ScopePreviewRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Preview document access for arbitrary scope params (unsaved)."""
    total = (await session.execute(select(func.count(Document.doc_id)).where(Document.status == "active"))).scalar_one()

    scope = KeyScope(
        scope_topic_ids=body.scope_topic_ids,
        scope_folder_ids=body.scope_folder_ids,
        permission_tier=body.permission_tier,
        tool_overrides={},
        max_snippet_chars=None,
    )
    fake_principal = Principal(type="api_key", id=admin.id, role="user", key_scope=scope)
    visible_query = apply_key_scope(
        select(func.count(Document.doc_id)).where(Document.status == "active"),
        fake_principal,
    )
    visible = (await session.execute(visible_query)).scalar_one()

    return ScopePreviewResponse(accessible_documents=visible, total_documents=total)


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
