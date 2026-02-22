"""First-time setup endpoint: create initial admin when no users exist."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.schemas.auth import LoginResponse, UserInfo
from harbor_clerk.auth import create_access_token, create_refresh_token, hash_password
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models import User
from harbor_clerk.models.enums import UserRole
from harbor_clerk.password_validation import validate_password

logger = logging.getLogger(__name__)
router = APIRouter(tags=["setup"])


class SetupRequest(BaseModel):
    email: str
    password: str


@router.post("/setup", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def setup(
    body: SetupRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Create the initial admin account. Only works when zero users exist."""
    count = await session.scalar(select(func.count()).select_from(User))
    if count and count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup already completed",
        )

    # Validate password
    errors = validate_password(body.password, body.email)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=errors,
        )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=UserRole.admin,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    # Update last_login_at
    await session.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await session.commit()

    logger.info("Initial admin created via setup: %s", body.email)

    access_token = create_access_token(user.user_id, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    settings = get_settings()
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.jwt_refresh_token_expire_days * 86400,
        path="/api/auth",
    )

    return LoginResponse(
        access_token=access_token,
        user=UserInfo(
            user_id=str(user.user_id),
            email=user.email,
            role=user.role.value,
            preferences=user.preferences or {},
        ),
    )
