"""User management endpoints (admin-only)."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.users import CreateUserRequest, UpdateUserRequest, UserResponse
from harbor_clerk.audit import log_audit
from harbor_clerk.auth import hash_password
from harbor_clerk.db import get_session
from harbor_clerk.models import User
from harbor_clerk.models.enums import UserRole
from harbor_clerk.password_validation import validate_password

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [
        UserResponse(
            user_id=str(u.user_id),
            email=u.email,
            role=u.role.value,
            is_active=u.is_active,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
        )
        for u in users
    ]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Validate role
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="Role must be 'admin' or 'user'")

    # Validate password
    pw_errors = validate_password(body.password, body.email)
    if pw_errors:
        raise HTTPException(status_code=422, detail=pw_errors)

    # Check duplicate email
    existing = await session.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
        )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=UserRole(body.role),
        is_active=True,
    )
    session.add(user)
    await log_audit(
        session, user_id=admin.id, action="create_user",
        target_type="user", target_id=user.user_id,
    )
    await session.commit()
    await session.refresh(user)

    return UserResponse(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserResponse(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if body.email is not None:
        dup = await session.execute(
            select(User).where(User.email == body.email, User.user_id != user_id)
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
        user.email = body.email

    if body.password is not None:
        email_for_check = body.email if body.email is not None else user.email
        pw_errors = validate_password(body.password, email_for_check)
        if pw_errors:
            raise HTTPException(status_code=422, detail=pw_errors)
        user.password_hash = hash_password(body.password)

    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=422, detail="Role must be 'admin' or 'user'")
        user.role = UserRole(body.role)

    if body.is_active is not None:
        user.is_active = body.is_active

    await log_audit(
        session, user_id=admin.id, action="update_user",
        target_type="user", target_id=user_id,
    )
    await session.commit()
    await session.refresh(user)

    return UserResponse(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Prevent deleting yourself
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete yourself")

    user.is_active = False
    await log_audit(
        session, user_id=admin.id, action="delete_user",
        target_type="user", target_id=user_id,
    )
    await session.commit()
