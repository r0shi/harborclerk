"""FastAPI authentication dependencies."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.auth import API_KEY_PREFIXES, decode_token, hash_api_key
from harbor_clerk.db import get_session
from harbor_clerk.models import ApiKey


@dataclass
class Principal:
    """Authenticated caller identity."""

    type: str  # "user" or "api_key"
    id: uuid.UUID  # user_id or key_id
    role: str  # "admin" or "user"


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:]
    return None


async def get_current_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Try JWT first, fall back to API key hash lookup."""
    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
        )

    # Try JWT decode
    if not token.startswith(API_KEY_PREFIXES):
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token type",
                )
            return Principal(
                type="user",
                id=uuid.UUID(payload["sub"]),
                role=payload["role"],
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
            )
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )

    # API key lookup
    key_hash = hash_api_key(token)
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    # Update last_used_at
    await session.execute(
        update(ApiKey)
        .where(ApiKey.key_id == api_key.key_id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return Principal(type="api_key", id=api_key.key_id, role="user")


async def require_user(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Any authenticated user (human or API key)."""
    return principal


async def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return principal


async def require_read_access(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Any authenticated principal (users + API keys) can read."""
    return principal
