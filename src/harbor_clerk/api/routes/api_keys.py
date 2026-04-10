"""API key management endpoints (admin-only)."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Date as SADate
from sqlalchemy import cast, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyOut,
    CreateApiKeyRequest,
    PatchApiKeyRequest,
    PurgeResponse,
    RequestLogPage,
    ScopePreviewRequest,
    ScopePreviewResponse,
    TimelineDay,
    UsageSummaryResponse,
)
from harbor_clerk.api.scope import KeyScope, apply_key_scope
from harbor_clerk.audit import log_audit
from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.db import get_session
from harbor_clerk.models import ApiKey
from harbor_clerk.models.api_request_log import ApiRequestLog
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


# ---------------------------------------------------------------------------
# Usage / Audit dashboard endpoints
# ---------------------------------------------------------------------------


@router.get("/api-keys/{key_id}/usage", response_model=UsageSummaryResponse)
async def get_key_usage(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    now = datetime.now(UTC)
    ts = ApiRequestLog.created_at
    st = ApiRequestLog.status

    # Single query: all request/error/denial counts + last_used via FILTER (WHERE ...)
    request_buckets = {"1h": 1, "6h": 6, "12h": 12, "24h": 24, "7d": 168, "30d": 720}
    error_buckets = {"1h": 1, "24h": 24, "7d": 168}

    columns = [func.max(ts).label("last_used")]
    labels = ["last_used"]
    for label, hours in request_buckets.items():
        cutoff = now - timedelta(hours=hours)
        columns.append(func.count(ApiRequestLog.request_id).filter(ts >= cutoff).label(f"req_{label}"))
        labels.append(f"req_{label}")
    for label, hours in error_buckets.items():
        cutoff = now - timedelta(hours=hours)
        columns.append(func.count(ApiRequestLog.request_id).filter(ts >= cutoff, st == "error").label(f"err_{label}"))
        columns.append(func.count(ApiRequestLog.request_id).filter(ts >= cutoff, st == "denied").label(f"den_{label}"))
        labels.extend([f"err_{label}", f"den_{label}"])

    row = (await session.execute(select(*columns).where(ApiRequestLog.api_key_id == key_id))).one()

    requests = {label: getattr(row, f"req_{label}") for label in request_buckets}
    errors = {label: getattr(row, f"err_{label}") for label in error_buckets}
    denials = {label: getattr(row, f"den_{label}") for label in error_buckets}

    # Top tools: still 6 queries (GROUP BY + ORDER BY + LIMIT can't be FILTERed)
    top_tools: dict[str, list[dict[str, object]]] = {}
    for label, hours in request_buckets.items():
        cutoff = now - timedelta(hours=hours)
        rows = (
            await session.execute(
                select(ApiRequestLog.endpoint, func.count(ApiRequestLog.request_id).label("cnt"))
                .where(ApiRequestLog.api_key_id == key_id, ts >= cutoff)
                .group_by(ApiRequestLog.endpoint)
                .order_by(func.count(ApiRequestLog.request_id).desc())
                .limit(10)
            )
        ).all()
        top_tools[label] = [{"endpoint": r[0], "count": r[1]} for r in rows]

    return {
        "requests": requests,
        "errors": errors,
        "denials": denials,
        "last_used_at": row.last_used,
        "top_tools": top_tools,
    }


@router.get("/api-keys/{key_id}/usage/timeline", response_model=list[TimelineDay])
async def get_key_timeline(
    key_id: uuid.UUID,
    days: int = 30,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if days < 1 or days > 90:
        raise HTTPException(status_code=422, detail="days must be between 1 and 90")
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                cast(ApiRequestLog.created_at, SADate).label("day"),
                ApiRequestLog.status,
                func.count(ApiRequestLog.request_id).label("cnt"),
            )
            .where(ApiRequestLog.api_key_id == key_id, ApiRequestLog.created_at >= cutoff)
            .group_by("day", ApiRequestLog.status)
            .order_by("day")
        )
    ).all()

    by_day: dict[str, dict[str, int]] = {}
    for day, stat, cnt in rows:
        d = str(day)
        if d not in by_day:
            by_day[d] = {"ok": 0, "error": 0, "denied": 0}
        if stat in by_day[d]:
            by_day[d][stat] = cnt
    return [{"date": d, **counts} for d, counts in sorted(by_day.items())]


@router.get("/api-keys/{key_id}/usage/requests", response_model=RequestLogPage)
async def get_key_requests(
    key_id: uuid.UUID,
    page: int = 1,
    page_size: int = 25,
    endpoint: str | None = None,
    status_filter: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if page_size > 200:
        page_size = 200
    if page < 1:
        page = 1
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    base = select(ApiRequestLog).where(ApiRequestLog.api_key_id == key_id)
    count_base = select(func.count(ApiRequestLog.request_id)).where(ApiRequestLog.api_key_id == key_id)
    if endpoint:
        base = base.where(ApiRequestLog.endpoint == endpoint)
        count_base = count_base.where(ApiRequestLog.endpoint == endpoint)
    if status_filter:
        base = base.where(ApiRequestLog.status == status_filter)
        count_base = count_base.where(ApiRequestLog.status == status_filter)
    if from_date:
        base = base.where(ApiRequestLog.created_at >= from_date)
        count_base = count_base.where(ApiRequestLog.created_at >= from_date)
    if to_date:
        base = base.where(ApiRequestLog.created_at <= to_date)
        count_base = count_base.where(ApiRequestLog.created_at <= to_date)

    total = (await session.execute(count_base)).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(ApiRequestLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return {
        "items": [
            {
                "request_id": str(r.request_id),
                "request_type": r.request_type,
                "endpoint": r.endpoint,
                "parameters": r.parameters,
                "status": r.status,
                "status_detail": r.status_detail,
                "result_summary": r.result_summary,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/api-keys/{key_id}/usage", response_model=PurgeResponse)
async def purge_key_usage(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    result = await session.execute(sa_delete(ApiRequestLog).where(ApiRequestLog.api_key_id == key_id))
    await log_audit(
        session,
        user_id=admin.id,
        action="purge_key_usage",
        target_type="api_key",
        target_id=key_id,
        detail={"deleted": result.rowcount},
    )
    await session.commit()
    return {"deleted": result.rowcount}
