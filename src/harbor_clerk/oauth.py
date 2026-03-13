"""OAuth 2.1 authorization server core — tokens, codes, PKCE, client registration."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.models.oauth_client import OAuthClient
from harbor_clerk.models.oauth_code import OAuthCode
from harbor_clerk.models.oauth_token import OAuthToken

# ---------------------------------------------------------------------------
# Token / secret helpers
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """Generate a 64-char hex token (32 random bytes)."""
    return secrets.token_hex(32)


def generate_client_secret() -> str:
    """Generate a URL-safe base64 client secret (48 random bytes)."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 hex digest — same pattern as API key hashing in auth.py."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _verify_code_challenge(verifier: str, challenge: str, method: str) -> bool:
    """Verify a PKCE code challenge. Only S256 is supported (OAuth 2.1).

    Uses authlib's RFC 7636 implementation for the S256 computation.
    """
    if method != "S256":
        return False
    return secrets.compare_digest(create_s256_code_challenge(verifier), challenge)


# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------


async def register_client(
    db: AsyncSession,
    *,
    redirect_uris: list[str],
    client_name: str | None = None,
    client_uri: str | None = None,
    grant_types: list[str] | None = None,
    response_types: list[str] | None = None,
    scope: str = "mcp",
) -> tuple[OAuthClient, str]:
    """Create an OAuth client with a hashed secret.

    Returns (client, raw_secret) so the caller can display the secret once.
    """
    raw_secret = generate_client_secret()
    client = OAuthClient(
        client_id=uuid.uuid4(),
        client_secret_hash=hash_token(raw_secret),
        client_name=client_name,
        redirect_uris=redirect_uris,
        grant_types=grant_types or ["authorization_code", "refresh_token"],
        response_types=response_types or ["code"],
        scope=scope,
        client_uri=client_uri,
    )
    db.add(client)
    await db.flush()
    return client, raw_secret


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------


async def create_authorization_code(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    user_id: uuid.UUID,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> str:
    """Create an authorization code record (expires in 10 min).

    Returns the raw code string.
    """
    raw_code = generate_token()
    code = OAuthCode(
        code_hash=hash_token(raw_code),
        client_id=client_id,
        user_id=user_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(code)
    await db.flush()
    return raw_code


async def validate_authorization_code(
    db: AsyncSession,
    *,
    code: str,
    client_id: uuid.UUID,
    redirect_uri: str,
    code_verifier: str,
) -> OAuthCode | None:
    """Validate an authorization code.

    Checks hash match, expiry, client_id, redirect_uri, PKCE, and marks used.
    Returns None on any failure.
    """
    code_hash = hash_token(code)
    result = await db.execute(select(OAuthCode).where(OAuthCode.code_hash == code_hash))
    record = result.scalar_one_or_none()
    if record is None:
        return None
    if record.used:
        return None
    if record.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return None
    if record.client_id != client_id:
        return None
    if record.redirect_uri != redirect_uri:
        return None
    if not _verify_code_challenge(code_verifier, record.code_challenge, record.code_challenge_method):
        return None

    # Mark as used
    record.used = True
    await db.flush()
    return record


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


async def issue_tokens(
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    user_id: uuid.UUID,
    scope: str,
) -> dict:
    """Issue an access + refresh token pair.

    Returns a dict matching the OAuth token response shape.
    """
    settings = get_settings()
    raw_access = generate_token()
    raw_refresh = generate_token()

    access_expires = datetime.now(UTC) + timedelta(minutes=settings.oauth_access_token_minutes)
    refresh_expires = datetime.now(UTC) + timedelta(days=settings.oauth_refresh_token_days)

    token = OAuthToken(
        access_token_hash=hash_token(raw_access),
        refresh_token_hash=hash_token(raw_refresh),
        client_id=client_id,
        user_id=user_id,
        scope=scope,
        access_token_expires_at=access_expires,
        refresh_token_expires_at=refresh_expires,
    )
    db.add(token)
    await db.flush()

    return {
        "access_token": raw_access,
        "token_type": "Bearer",
        "expires_in": settings.oauth_access_token_minutes * 60,
        "refresh_token": raw_refresh,
        "scope": scope,
    }


async def refresh_access_token(
    db: AsyncSession,
    *,
    refresh_token: str,
    client_id: uuid.UUID,
) -> dict | None:
    """Validate a refresh token and rotate: revoke old pair, issue new pair.

    Returns None on failure.
    """
    refresh_hash = hash_token(refresh_token)
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.refresh_token_hash == refresh_hash,
            OAuthToken.client_id == client_id,
            OAuthToken.revoked.is_(False),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        return None

    # Check refresh token expiry
    if record.refresh_token_expires_at is None:
        return None
    if record.refresh_token_expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return None

    # Revoke old token pair (rotation)
    record.revoked = True
    await db.flush()

    # Issue new pair
    return await issue_tokens(
        db,
        client_id=record.client_id,
        user_id=record.user_id,
        scope=record.scope,
    )


# ---------------------------------------------------------------------------
# Token validation (for MCP middleware)
# ---------------------------------------------------------------------------


async def validate_access_token(
    db: AsyncSession,
    token: str,
) -> tuple[uuid.UUID, str] | None:
    """Look up an access token by hash, check expiry + not revoked.

    Updates last_used_at on success. Returns (user_id, role) or None.
    """
    from harbor_clerk.models.user import User

    access_hash = hash_token(token)
    result = await db.execute(
        select(OAuthToken, User.role)
        .join(User, OAuthToken.user_id == User.user_id)
        .where(
            OAuthToken.access_token_hash == access_hash,
            OAuthToken.revoked.is_(False),
        )
    )
    row = result.one_or_none()
    if row is None:
        return None

    oauth_token, role = row
    if oauth_token.access_token_expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return None

    # Update last_used_at
    await db.execute(
        update(OAuthToken).where(OAuthToken.token_id == oauth_token.token_id).values(last_used_at=datetime.now(UTC))
    )
    await db.flush()

    return oauth_token.user_id, role.value if hasattr(role, "value") else role


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def revoke_token(
    db: AsyncSession,
    *,
    token: str,
    token_type_hint: str | None = None,
) -> bool:
    """Revoke a token (access or refresh).

    Tries access hash first (unless hinted as refresh), then refresh hash.
    Returns True if found and revoked.
    """
    token_hash = hash_token(token)

    # Determine lookup order based on hint
    if token_type_hint == "refresh_token":
        lookups = [
            OAuthToken.refresh_token_hash == token_hash,
            OAuthToken.access_token_hash == token_hash,
        ]
    else:
        lookups = [
            OAuthToken.access_token_hash == token_hash,
            OAuthToken.refresh_token_hash == token_hash,
        ]

    for condition in lookups:
        result = await db.execute(select(OAuthToken).where(condition, OAuthToken.revoked.is_(False)))
        record = result.scalar_one_or_none()
        if record is not None:
            record.revoked = True
            await db.flush()
            return True

    return False


# ---------------------------------------------------------------------------
# Client lookup
# ---------------------------------------------------------------------------


async def get_client(
    db: AsyncSession,
    client_id: uuid.UUID,
) -> OAuthClient | None:
    """Look up an OAuth client by client_id."""
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


def verify_client_secret(raw_secret: str, client: OAuthClient) -> bool:
    """Timing-safe comparison of hashed client secret."""
    if client.client_secret_hash is None:
        return False
    return secrets.compare_digest(hash_token(raw_secret), client.client_secret_hash)
