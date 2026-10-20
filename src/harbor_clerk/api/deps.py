"""FastAPI authentication dependencies."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.scope import REST_ROUTE_TOOLS, KeyScope, UserScope
from harbor_clerk.auth import API_KEY_PREFIXES, decode_token, hash_api_key
from harbor_clerk.db import get_session
from harbor_clerk.models import ApiKey, User


@dataclass
class Principal:
    """Authenticated caller identity."""

    type: str  # "user" or "api_key"
    id: uuid.UUID  # user_id or key_id
    role: str  # "admin" or "user"
    key_scope: KeyScope | None = None  # populated for api_key principals only
    user_scope: UserScope | None = None  # populated for human-user principals when folder scope is active


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
            user_id = uuid.UUID(payload["sub"])
            # Password-change revocation: tokens issued before the user's
            # password_changed_at are rejected. Closes the gap where an
            # exfiltrated access token survived a password rotation done
            # to recover from the exfiltration. Pre-fix migration sets
            # password_changed_at = now() on existing rows, so no
            # currently-valid token gets retroactively killed.
            #
            # jwt.decode returns `iat` as a Unix timestamp (int). Tokens
            # minted before 0021 don't have an `iat` claim — for safety,
            # treat the absence as "older than any password_changed_at"
            # and reject. The legacy access token's max lifetime is the
            # configured `jwt_access_token_expire_minutes`, so this drift
            # heals on its own within that window.
            iat_value = payload.get("iat")
            if iat_value is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token missing iat claim — re-authenticate",
                )
            token_iat = datetime.fromtimestamp(iat_value, tz=UTC)
            user_row = (
                await session.execute(select(User.password_changed_at, User.role).where(User.user_id == user_id))
            ).one_or_none()
            if user_row is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found",
                )
            password_changed_at, db_role = user_row
            # Truncate password_changed_at to whole seconds before
            # comparing. JWT's `iat` is int seconds (no sub-second
            # precision), while password_changed_at is microsecond-
            # precision. Subtracting a 1s grace would compare at the right
            # resolution but also leave a real 1s attacker window after a
            # password change. Floor-then-compare gives the same false-
            # positive protection AND closes the window: a token with
            # iat=floor(T) is rejected if the password changed at any
            # time during second T (inclusive).
            password_changed_at_floor = password_changed_at.replace(microsecond=0)
            if token_iat < password_changed_at_floor:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token revoked by password change",
                )
            # Trust DB role over JWT — a role change is also a revocation
            # event in spirit. JWT-embedded role would otherwise let a
            # demoted admin keep admin until token expiry. `db_role` is
            # always a UserRole enum here (str + enum.Enum) via the
            # typed Mapped[UserRole] column.
            return Principal(
                type="user",
                id=user_id,
                role=db_role.value,
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
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True)))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    # Expiry check
    if api_key.expires_at is not None and api_key.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key expired",
        )
    # Update last_used_at
    await session.execute(update(ApiKey).where(ApiKey.key_id == api_key.key_id).values(last_used_at=datetime.now(UTC)))
    await session.commit()
    scope = KeyScope(
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        max_snippet_chars=api_key.max_snippet_chars,
        rate_limit_rpm=api_key.rate_limit_rpm,
        rate_limit_rph=api_key.rate_limit_rph,
    )
    return Principal(type="api_key", id=api_key.key_id, role="user", key_scope=scope)


async def require_human_user(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Human user only — rejects API keys.

    Use this for mutating endpoints any signed-in human may call (uploads,
    chat, research), to enforce the project's read-only API key contract, and
    for reads that have no MCP counterpart (jobs, stats, watched folders, chat,
    research). A write that shapes the corpus for everyone (watched folders,
    mail accounts, document delete and reprocess) is ``require_admin``, which
    rejects keys as well (#852). There is no
    "any authenticated principal" dependency on purpose: a key reaches a route
    only through ``require_read_access`` and its ``REST_ROUTE_TOOLS`` entry.
    """
    if principal.type == "api_key":
        detail = "API keys cannot access this endpoint"
        # ApiKeyRequestLogMiddleware reads this so the audit log records a denial, as MCP does.
        request.state.api_key_denied = detail
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
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
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Human users, and API keys whose scope includes the tool that governs this route.

    Humans pass unconditionally. An API key passes only if the route is listed
    in ``REST_ROUTE_TOOLS`` and the MCP tool it maps to is in the key's
    effective tool set — the same tier-plus-overrides check that
    ``ScopedFastMCP.call_tool`` applies — so a search-tier key that cannot call
    ``kb_get_document`` over MCP cannot fetch document detail over REST either
    (#646). A route that uses this dependency without a table entry is closed
    to keys: fail closed, never open.
    """
    if principal.type != "api_key":
        return principal
    route = request.scope.get("route")
    tool = REST_ROUTE_TOOLS.get((request.method, getattr(route, "path", "")))
    if tool is None:
        detail = "This endpoint is not available to API keys"
    elif principal.key_scope is None or tool not in principal.key_scope.effective_tools:
        tier = principal.key_scope.permission_tier if principal.key_scope is not None else "unknown"
        detail = f"API key tier '{tier}' does not include {tool}, which governs this endpoint"
    else:
        return principal
    # ApiKeyRequestLogMiddleware reads this so the audit log records a denial, as MCP does.
    request.state.api_key_denied = detail
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
