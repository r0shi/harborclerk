"""Authentication endpoints: login, refresh, logout, me."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, get_current_principal
from harbor_clerk.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    PreferencesUpdate,
    UserInfo,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models import User
from harbor_clerk.password_validation import validate_password

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _is_secure_request(request: Request) -> bool:
    """Check if the request came over HTTPS (directly or via reverse proxy)."""
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
        )

    # Update last_login_at
    await session.execute(update(User).where(User.user_id == user.user_id).values(last_login_at=datetime.now(UTC)))
    await log_audit(
        session,
        user_id=user.user_id,
        action="login",
        target_type="user",
        target_id=user.user_id,
    )
    await session.commit()

    access_token = create_access_token(user.user_id, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    settings = get_settings()
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_is_secure_request(request),
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


@router.post("/auth/refresh")
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )
    try:
        payload = decode_token(refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
            )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = payload["sub"]
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled",
        )

    access_token = create_access_token(user.user_id, user.role.value)
    new_refresh = create_refresh_token(user.user_id)

    settings = get_settings()
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="strict",
        max_age=settings.jwt_refresh_token_expire_days * 86400,
        path="/api/auth",
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key="refresh_token", path="/api/auth")
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserInfo)
async def get_me(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
):
    if principal.type != "user":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API keys cannot access /me",
        )
    result = await session.execute(select(User).where(User.user_id == principal.id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserInfo(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role.value,
        preferences=user.preferences or {},
    )


@router.patch("/me/preferences", response_model=UserInfo)
async def update_preferences(
    body: PreferencesUpdate,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
):
    if principal.type != "user":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API keys cannot update preferences",
        )
    # Validate values
    if body.theme is not None and body.theme not in ("light", "dark"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="theme must be 'light' or 'dark'",
        )
    if body.page_size is not None and body.page_size not in (10, 25, 50, 100):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be 10, 25, 50, or 100",
        )

    result = await session.execute(select(User).where(User.user_id == principal.id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    prefs = dict(user.preferences or {})
    if body.theme is not None:
        prefs["theme"] = body.theme
    if body.page_size is not None:
        prefs["page_size"] = body.page_size

    await session.execute(update(User).where(User.user_id == principal.id).values(preferences=prefs))
    await session.commit()
    await session.refresh(user)

    return UserInfo(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role.value,
        preferences=user.preferences or {},
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
):
    """Change the authenticated user's password.

    Requires the current password to be supplied. The new password is
    validated by `password_validation.validate_password` and stored as a
    fresh bcrypt hash. The existing access token remains valid until
    expiry; the refresh-token cookie is unchanged. Other sessions for
    this user are NOT invalidated by this endpoint — that's a follow-up
    when there's a session-revocation surface to plumb into.
    """
    if principal.type != "user":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API keys cannot change a user's password",
        )

    result = await session.execute(select(User).where(User.user_id == principal.id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Verify the current password before allowing the change. This both
    # confirms the request is from a still-authorised holder of the
    # password (not just someone who stole an access token) and gives a
    # clear error path if the user is mistaken about what their current
    # password is.
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    # Reject reusing the same password
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must differ from the current password",
        )

    errors = validate_password(body.new_password, user.email)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": errors},
        )

    new_hash = hash_password(body.new_password)
    await session.execute(
        update(User).where(User.user_id == principal.id).values(password_hash=new_hash),
    )
    await log_audit(
        session,
        user_id=user.user_id,
        action="change_password",
        target_type="user",
        target_id=user.user_id,
    )
    await session.commit()
    return None
