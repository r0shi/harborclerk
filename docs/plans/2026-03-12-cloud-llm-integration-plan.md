# OAuth 2.1 for MCP Clients — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an OAuth 2.1 authorization server to Harbor Clerk so ChatGPT (and other MCP clients) can authenticate and access the knowledge base, plus an Integrations page with connection guides.

**Architecture:** Authlib provides the OAuth 2.1 authorization server (authorization code grant + PKCE + dynamic client registration). Three new DB tables store clients, codes, and tokens. MCP middleware is extended to accept OAuth access tokens. A server-rendered consent page handles the authorization flow. A React Integrations page shows active connections and setup guides.

**Tech Stack:** Authlib 1.6+, FastAPI, SQLAlchemy async, Alembic, React/TypeScript/Tailwind

---

## Reference: Key Files

| File | Purpose |
|---|---|
| `src/harbor_clerk/config.py` | Settings class (pydantic-settings) |
| `src/harbor_clerk/auth.py` | JWT/password/API key helpers |
| `src/harbor_clerk/api/app.py` | FastAPI app factory, router mounting |
| `src/harbor_clerk/api/deps.py` | `Principal` dataclass, `get_current_principal()` |
| `src/harbor_clerk/mcp_server.py` | MCP ASGI app, `MCPAuthMiddleware`, `_resolve_principal()` |
| `src/harbor_clerk/models/__init__.py` | Model registry (import all models for Alembic) |
| `src/harbor_clerk/models/base.py` | `Base`, `uuid_pk`, `created_at` annotated types |
| `src/harbor_clerk/models/api_key.py` | `ApiKey` model (pattern to follow) |
| `frontend/src/App.tsx` | React router setup |
| `frontend/src/components/Layout.tsx` | Nav bar with `TabLink` components |
| `tests/test_mcp_auth.py` | MCP auth test patterns |
| `alembic/versions/` | Migrations (currently 0001–0005) |

## Reference: Auth Patterns

**API key hashing:** `hashlib.sha256(raw_key.encode()).hexdigest()` — use this same pattern for OAuth secrets/tokens.

**Principal dataclass:** `Principal(type="user"|"api_key", id=UUID, role="admin"|"user")` — OAuth tokens will produce `Principal(type="oauth", id=user_id, role=user.role)`.

**MCP token resolution:** `_resolve_principal(token)` in `mcp_server.py` checks API key prefixes first, then decodes JWT. Add OAuth token lookup as a third path.

---

### Task 1: Add authlib dependency

**Files:**
- Modify: `pyproject.toml:6-28`

**Step 1: Add authlib to dependencies**

In `pyproject.toml`, add `"authlib>=1.6.0"` to the `dependencies` list (after `"PyJWT>=2.9.0"`):

```toml
    "PyJWT>=2.9.0",
    "authlib>=1.6.0",
```

**Step 2: Install**

Run: `cd /Users/alex/mcp-gateway && uv sync`
Expected: authlib installed successfully

**Step 3: Verify import works**

Run: `cd /Users/alex/mcp-gateway && uv run python -c "import authlib; print(authlib.__version__)"`
Expected: prints version (1.6.x)

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add authlib dependency for OAuth 2.1 support"
```

---

### Task 2: Add OAuth config settings

**Files:**
- Modify: `src/harbor_clerk/config.py`
- Test: `tests/test_oauth.py` (create)

**Step 1: Write the failing test**

Create `tests/test_oauth.py`:

```python
"""OAuth 2.1 tests."""

import os

import pytest


class TestOAuthConfig:
    def test_default_refresh_token_days(self):
        from harbor_clerk.config import Settings

        s = Settings(
            secret_key="test",
            database_url="postgresql+asyncpg://x:x@localhost/x",
        )
        assert s.oauth_refresh_token_days == 90

    def test_custom_refresh_token_days(self):
        from harbor_clerk.config import Settings

        s = Settings(
            secret_key="test",
            database_url="postgresql+asyncpg://x:x@localhost/x",
            oauth_refresh_token_days=30,
        )
        assert s.oauth_refresh_token_days == 30

    def test_public_url_default_empty(self):
        from harbor_clerk.config import Settings

        s = Settings(
            secret_key="test",
            database_url="postgresql+asyncpg://x:x@localhost/x",
        )
        assert s.public_url == ""

    def test_public_url_strips_trailing_slash(self):
        from harbor_clerk.config import Settings

        s = Settings(
            secret_key="test",
            database_url="postgresql+asyncpg://x:x@localhost/x",
            public_url="https://example.com/",
        )
        assert s.public_url == "https://example.com"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_oauth.py::TestOAuthConfig -v`
Expected: FAIL — `public_url` and `oauth_refresh_token_days` not in Settings

**Step 3: Add settings fields**

In `src/harbor_clerk/config.py`, add to the `Settings` class (after the existing JWT fields):

```python
    # OAuth 2.1
    public_url: str = Field(default="")
    oauth_refresh_token_days: int = Field(default=90)
    oauth_access_token_minutes: int = Field(default=60)
```

Add a validator to strip trailing slash from `public_url`:

```python
    @field_validator("public_url", mode="before")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if isinstance(v, str) else v
```

(Import `field_validator` from pydantic at the top of the file if not already imported.)

**Step 4: Run test to verify it passes**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_oauth.py::TestOAuthConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_oauth.py
git commit -m "feat(oauth): add public_url and oauth_refresh_token_days settings"
```

---

### Task 3: Create OAuth database models

**Files:**
- Create: `src/harbor_clerk/models/oauth_client.py`
- Create: `src/harbor_clerk/models/oauth_code.py`
- Create: `src/harbor_clerk/models/oauth_token.py`
- Modify: `src/harbor_clerk/models/__init__.py`

**Step 1: Create OAuthClient model**

Create `src/harbor_clerk/models/oauth_client.py`:

```python
"""OAuth 2.1 dynamic client registration."""

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[uuid_pk]
    client_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False)
    grant_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    response_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="mcp")
    client_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
```

**Step 2: Create OAuthCode model**

Create `src/harbor_clerk/models/oauth_code.py`:

```python
"""OAuth 2.1 authorization codes."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_id: Mapped[uuid_pk]
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid_pk] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oauth_clients.client_id"), nullable=False
    )
    user_id: Mapped[uuid_pk] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(Text, nullable=False, server_default="S256")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[created_at]
```

**Step 3: Create OAuthToken model**

Create `src/harbor_clerk/models/oauth_token.py`:

```python
"""OAuth 2.1 access and refresh tokens."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    token_id: Mapped[uuid_pk]
    access_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[uuid_pk] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oauth_clients.client_id"), nullable=False
    )
    user_id: Mapped[uuid_pk] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[created_at]
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

**Step 4: Register models in `__init__.py`**

In `src/harbor_clerk/models/__init__.py`, add imports and `__all__` entries:

```python
from harbor_clerk.models.oauth_client import OAuthClient
from harbor_clerk.models.oauth_code import OAuthCode
from harbor_clerk.models.oauth_token import OAuthToken
```

Add `"OAuthClient"`, `"OAuthCode"`, `"OAuthToken"` to the `__all__` list.

**Step 5: Verify models import cleanly**

Run: `cd /Users/alex/mcp-gateway && uv run python -c "from harbor_clerk.models import OAuthClient, OAuthCode, OAuthToken; print('OK')"`
Expected: `OK`

**Step 6: Commit**

```bash
git add src/harbor_clerk/models/oauth_client.py src/harbor_clerk/models/oauth_code.py src/harbor_clerk/models/oauth_token.py src/harbor_clerk/models/__init__.py
git commit -m "feat(oauth): add OAuthClient, OAuthCode, OAuthToken models"
```

---

### Task 4: Create Alembic migration

**Files:**
- Create: `alembic/versions/0006_oauth_tables.py`

**Step 1: Write migration**

Create `alembic/versions/0006_oauth_tables.py`:

```python
"""Add OAuth 2.1 tables for MCP client authorization.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("client_secret_hash", sa.Text, nullable=True),
        sa.Column("client_name", sa.Text, nullable=True),
        sa.Column("redirect_uris", JSONB, nullable=False),
        sa.Column("grant_types", JSONB, nullable=False),
        sa.Column("response_types", JSONB, nullable=False),
        sa.Column("scope", sa.Text, nullable=False, server_default="mcp"),
        sa.Column("client_uri", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "oauth_codes",
        sa.Column("code_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code_hash", sa.Text, nullable=False),
        sa.Column("client_id", UUID(as_uuid=True), sa.ForeignKey("oauth_clients.client_id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("code_challenge", sa.Text, nullable=False),
        sa.Column("code_challenge_method", sa.Text, nullable=False, server_default="S256"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "oauth_tokens",
        sa.Column("token_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("access_token_hash", sa.Text, nullable=False),
        sa.Column("refresh_token_hash", sa.Text, nullable=True),
        sa.Column("client_id", UUID(as_uuid=True), sa.ForeignKey("oauth_clients.client_id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_oauth_tokens_access_hash", "oauth_tokens", ["access_token_hash"])
    op.create_index("ix_oauth_tokens_refresh_hash", "oauth_tokens", ["refresh_token_hash"])
    op.create_index("ix_oauth_codes_code_hash", "oauth_codes", ["code_hash"])


def downgrade() -> None:
    op.drop_index("ix_oauth_codes_code_hash")
    op.drop_index("ix_oauth_tokens_refresh_hash")
    op.drop_index("ix_oauth_tokens_access_hash")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_clients")
```

**Step 2: Verify migration syntax**

Run: `cd /Users/alex/mcp-gateway && uv run python -c "import alembic.versions; print('syntax ok')" 2>/dev/null || uv run python -c "import importlib.util; spec = importlib.util.spec_from_file_location('m', 'alembic/versions/0006_oauth_tables.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print('OK')"`

**Step 3: Commit**

```bash
git add alembic/versions/0006_oauth_tables.py
git commit -m "feat(oauth): add migration for oauth_clients, oauth_codes, oauth_tokens"
```

---

### Task 5: Implement OAuth server core with Authlib

**Files:**
- Create: `src/harbor_clerk/oauth.py`
- Test: `tests/test_oauth.py` (append)

**Step 1: Write failing tests for OAuth token helpers**

Append to `tests/test_oauth.py`:

```python
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


class TestOAuthHelpers:
    def test_generate_oauth_token(self):
        from harbor_clerk.oauth import generate_token

        token = generate_token()
        assert len(token) == 64  # 32 bytes hex
        assert isinstance(token, str)

    def test_hash_oauth_token(self):
        from harbor_clerk.oauth import hash_token

        token = "abcdef1234567890"
        result = hash_token(token)
        assert result == hashlib.sha256(token.encode()).hexdigest()

    def test_generate_client_secret(self):
        from harbor_clerk.oauth import generate_client_secret

        secret = generate_client_secret()
        assert len(secret) > 32
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_oauth.py::TestOAuthHelpers -v`
Expected: FAIL — module `harbor_clerk.oauth` does not exist

**Step 3: Implement OAuth server module**

Create `src/harbor_clerk/oauth.py`:

```python
"""OAuth 2.1 authorization server for MCP client authentication.

Uses Authlib for the authorization code grant with PKCE.
Tokens are opaque (random hex strings) hashed with SHA-256 for storage.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.models.oauth_client import OAuthClient
from harbor_clerk.models.oauth_code import OAuthCode
from harbor_clerk.models.oauth_token import OAuthToken
from harbor_clerk.models.user import User


def generate_token() -> str:
    """Generate a cryptographically random 64-char hex token."""
    return secrets.token_hex(32)


def generate_client_secret() -> str:
    """Generate a cryptographically random client secret."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 hash for token storage (same pattern as API keys)."""
    return hashlib.sha256(token.encode()).hexdigest()


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
    """Register a new OAuth client. Returns (client, raw_client_secret)."""
    raw_secret = generate_client_secret()
    client = OAuthClient(
        client_secret_hash=hash_token(raw_secret),
        client_name=client_name,
        redirect_uris=redirect_uris,
        grant_types=grant_types or ["authorization_code"],
        response_types=response_types or ["code"],
        scope=scope,
        client_uri=client_uri,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return client, raw_secret


# ---------------------------------------------------------------------------
# Authorization code
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
    """Create and store an authorization code. Returns the raw code."""
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
    await db.commit()
    return raw_code


async def validate_authorization_code(
    db: AsyncSession,
    *,
    code: str,
    client_id: uuid.UUID,
    redirect_uri: str,
    code_verifier: str,
) -> OAuthCode | None:
    """Validate and consume an authorization code. Returns the code record or None."""
    code_hash = hash_token(code)
    result = await db.execute(
        select(OAuthCode).where(
            OAuthCode.code_hash == code_hash,
            OAuthCode.client_id == client_id,
            OAuthCode.used.is_(False),
        )
    )
    auth_code = result.scalar_one_or_none()
    if auth_code is None:
        return None

    # Check expiry
    if auth_code.expires_at < datetime.now(UTC):
        return None

    # Check redirect_uri matches
    if auth_code.redirect_uri != redirect_uri:
        return None

    # Verify PKCE
    if not _verify_code_challenge(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
        return None

    # Mark as used
    auth_code.used = True
    await db.commit()
    return auth_code


def _verify_code_challenge(verifier: str, challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method != "S256":
        return False
    import base64

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, challenge)


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
    """Issue access + refresh tokens. Returns dict with raw tokens and expiry."""
    settings = get_settings()
    raw_access = generate_token()
    raw_refresh = generate_token()
    now = datetime.now(UTC)

    token = OAuthToken(
        access_token_hash=hash_token(raw_access),
        refresh_token_hash=hash_token(raw_refresh),
        client_id=client_id,
        user_id=user_id,
        scope=scope,
        access_token_expires_at=now + timedelta(minutes=settings.oauth_access_token_minutes),
        refresh_token_expires_at=now + timedelta(days=settings.oauth_refresh_token_days),
    )
    db.add(token)
    await db.commit()

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
    """Validate refresh token, rotate, and issue new access token. Returns None on failure."""
    refresh_hash = hash_token(refresh_token)
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.refresh_token_hash == refresh_hash,
            OAuthToken.client_id == client_id,
            OAuthToken.revoked.is_(False),
        )
    )
    old_token = result.scalar_one_or_none()
    if old_token is None:
        return None

    # Check refresh token expiry
    if old_token.refresh_token_expires_at and old_token.refresh_token_expires_at < datetime.now(UTC):
        return None

    # Revoke old token (rotation)
    old_token.revoked = True
    await db.flush()

    # Issue new tokens
    return await issue_tokens(
        db,
        client_id=old_token.client_id,
        user_id=old_token.user_id,
        scope=old_token.scope,
    )


# ---------------------------------------------------------------------------
# Token validation (for MCP middleware)
# ---------------------------------------------------------------------------


async def validate_access_token(
    db: AsyncSession,
    token: str,
) -> tuple[uuid.UUID, str] | None:
    """Validate an OAuth access token. Returns (user_id, role) or None."""
    token_hash = hash_token(token)
    result = await db.execute(
        select(OAuthToken, User).join(User, OAuthToken.user_id == User.user_id).where(
            OAuthToken.access_token_hash == token_hash,
            OAuthToken.revoked.is_(False),
        )
    )
    row = result.one_or_none()
    if row is None:
        return None

    oauth_token, user = row.tuple()

    # Check expiry
    if oauth_token.access_token_expires_at < datetime.now(UTC):
        return None

    # Update last_used_at
    oauth_token.last_used_at = datetime.now(UTC)
    await db.commit()

    return user.user_id, user.role.value


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def revoke_token(
    db: AsyncSession,
    *,
    token: str,
    token_type_hint: str | None = None,
) -> bool:
    """Revoke an access or refresh token. Returns True if found and revoked."""
    token_hash = hash_token(token)

    # Try access token first (or if hinted)
    if token_type_hint != "refresh_token":
        result = await db.execute(
            select(OAuthToken).where(OAuthToken.access_token_hash == token_hash)
        )
        found = result.scalar_one_or_none()
        if found:
            found.revoked = True
            await db.commit()
            return True

    # Try refresh token
    if token_type_hint != "access_token":
        result = await db.execute(
            select(OAuthToken).where(OAuthToken.refresh_token_hash == token_hash)
        )
        found = result.scalar_one_or_none()
        if found:
            found.revoked = True
            await db.commit()
            return True

    return False


# ---------------------------------------------------------------------------
# Client lookup
# ---------------------------------------------------------------------------


async def get_client(db: AsyncSession, client_id: uuid.UUID) -> OAuthClient | None:
    """Look up an OAuth client by ID."""
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


def verify_client_secret(raw_secret: str, client: OAuthClient) -> bool:
    """Verify a client secret against stored hash."""
    if client.client_secret_hash is None:
        return False
    return secrets.compare_digest(hash_token(raw_secret), client.client_secret_hash)
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_oauth.py -v`
Expected: All tests PASS

**Step 5: Lint and format**

Run: `cd /Users/alex/mcp-gateway && uv run ruff check src/harbor_clerk/oauth.py && uv run ruff format src/harbor_clerk/oauth.py`

**Step 6: Commit**

```bash
git add src/harbor_clerk/oauth.py tests/test_oauth.py
git commit -m "feat(oauth): implement OAuth server core — tokens, codes, PKCE, client registration"
```

---

### Task 6: Implement OAuth API routes

**Files:**
- Create: `src/harbor_clerk/api/routes/oauth.py`
- Modify: `src/harbor_clerk/api/app.py`

**Step 1: Create OAuth routes**

Create `src/harbor_clerk/api/routes/oauth.py`:

```python
"""OAuth 2.1 endpoints for MCP client authorization.

Endpoints:
  GET  /.well-known/oauth-protected-resource   — Resource metadata
  GET  /.well-known/oauth-authorization-server  — Authorization server metadata
  POST /oauth/register                          — Dynamic client registration
  GET  /oauth/authorize                         — Authorization (consent page)
  POST /oauth/authorize                         — Authorization (form submit)
  POST /oauth/token                             — Token exchange
  POST /oauth/revoke                            — Token revocation
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import get_session
from harbor_clerk.auth import hash_api_key, verify_password
from harbor_clerk.config import get_settings
from harbor_clerk.models.user import User
from harbor_clerk.oauth import (
    create_authorization_code,
    get_client,
    issue_tokens,
    refresh_access_token,
    register_client,
    revoke_token,
    validate_authorization_code,
    verify_client_secret,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_public_url() -> str:
    """Return the configured public URL or raise 503."""
    settings = get_settings()
    if not settings.public_url:
        raise HTTPException(
            status_code=503,
            detail="Public URL not configured. Set it in System Settings to enable OAuth.",
        )
    return settings.public_url


# ---------------------------------------------------------------------------
# Well-known metadata
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata():
    base = _require_public_url()
    return {
        "resource": base,
        "authorization_servers": [base],
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    base = _require_public_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": ["mcp"],
    }


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


class ClientRegistrationRequest(BaseModel):
    redirect_uris: list[str]
    client_name: str | None = None
    client_uri: str | None = None
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    scope: str | None = None


@router.post("/oauth/register", status_code=201)
async def register(
    body: ClientRegistrationRequest,
    db: AsyncSession = Depends(get_session),
):
    # TODO: rate limiting (10/hr per IP)
    client, raw_secret = await register_client(
        db,
        redirect_uris=body.redirect_uris,
        client_name=body.client_name,
        client_uri=body.client_uri,
        grant_types=body.grant_types,
        response_types=body.response_types,
        scope=body.scope or "mcp",
    )
    return {
        "client_id": str(client.client_id),
        "client_secret": raw_secret,
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": client.grant_types,
        "response_types": client.response_types,
        "scope": client.scope,
    }


# ---------------------------------------------------------------------------
# Authorization endpoint (consent page)
# ---------------------------------------------------------------------------


_CONSENT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize — Harbor Clerk</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f7; display: flex; justify-content: center; align-items: center;
         min-height: 100vh; color: #1d1d1f; }
  .card { background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
          padding: 40px; max-width: 420px; width: 100%; }
  .logo { text-align: center; margin-bottom: 24px; font-size: 24px; font-weight: 700; }
  .client-name { text-align: center; font-size: 15px; color: #6e6e73; margin-bottom: 24px; }
  .client-name strong { color: #1d1d1f; }
  .scope { background: #f5f5f7; border-radius: 8px; padding: 12px 16px; margin-bottom: 24px;
           font-size: 13px; color: #6e6e73; }
  .scope strong { color: #1d1d1f; display: block; margin-bottom: 4px; }
  label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: #6e6e73; }
  input[type=email], input[type=password] {
    width: 100%; padding: 10px 12px; border: 1px solid #d2d2d7; border-radius: 8px;
    font-size: 15px; margin-bottom: 16px; outline: none; }
  input:focus { border-color: #0071e3; box-shadow: 0 0 0 3px rgba(0,113,227,0.15); }
  .error { background: #fff2f2; color: #d70015; padding: 10px 12px; border-radius: 8px;
           font-size: 13px; margin-bottom: 16px; }
  .actions { display: flex; gap: 12px; margin-top: 8px; }
  button { flex: 1; padding: 10px; border: none; border-radius: 8px; font-size: 15px;
           font-weight: 500; cursor: pointer; }
  .btn-authorize { background: #0071e3; color: #fff; }
  .btn-authorize:hover { background: #0077ED; }
  .btn-deny { background: #e8e8ed; color: #1d1d1f; }
  .btn-deny:hover { background: #d2d2d7; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">Harbor Clerk</div>
  <div class="client-name"><strong>{client_name}</strong> wants to access your knowledge base</div>
  <div class="scope">
    <strong>Permissions requested:</strong>
    Read-only access to search, read passages, and explore your document collection via MCP.
  </div>
  {error_html}
  <form method="post" action="/oauth/authorize">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="scope" value="{scope}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <input type="hidden" name="response_type" value="code">
    <label for="email">Email</label>
    <input type="email" id="email" name="email" required autocomplete="email" value="{email}">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <div class="actions">
      <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
      <button type="submit" name="action" value="authorize" class="btn-authorize">Authorize</button>
    </div>
  </form>
</div>
</body>
</html>
"""


def _render_consent(
    client_name: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str,
    error: str = "",
    email: str = "",
) -> HTMLResponse:
    error_html = f'<div class="error">{error}</div>' if error else ""
    html = _CONSENT_HTML.format(
        client_name=client_name or "An application",
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        error_html=error_html,
        email=email,
    )
    return HTMLResponse(content=html)


@router.get("/oauth/authorize")
async def authorize_get(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(""),
    scope: str = Query("mcp"),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    db: AsyncSession = Depends(get_session),
):
    _require_public_url()

    if response_type != "code":
        raise HTTPException(400, "Unsupported response_type")
    if code_challenge_method != "S256":
        raise HTTPException(400, "Only S256 code_challenge_method is supported")

    # Validate client
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        raise HTTPException(400, "Invalid client_id")

    client = await get_client(db, cid)
    if client is None:
        raise HTTPException(400, "Unknown client_id")

    if redirect_uri not in client.redirect_uris:
        raise HTTPException(400, "Invalid redirect_uri")

    return _render_consent(
        client_name=client.client_name,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )


@router.post("/oauth/authorize")
async def authorize_post(
    action: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    scope: str = Form("mcp"),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    email: str = Form(""),
    password: str = Form(""),
    response_type: str = Form("code"),
    db: AsyncSession = Depends(get_session),
):
    _require_public_url()

    # Validate client
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        raise HTTPException(400, "Invalid client_id")

    client = await get_client(db, cid)
    if client is None:
        raise HTTPException(400, "Unknown client_id")

    if redirect_uri not in client.redirect_uris:
        raise HTTPException(400, "Invalid redirect_uri")

    # User denied
    if action == "deny":
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            url=f"{redirect_uri}{sep}error=access_denied&state={state}",
            status_code=302,
        )

    # Authenticate user
    from sqlalchemy import select

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return _render_consent(
            client_name=client.client_name,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            error="Invalid email or password.",
            email=email,
        )

    # Only admins can authorize OAuth clients
    if user.role.value != "admin":
        return _render_consent(
            client_name=client.client_name,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            error="Only administrators can authorize external applications.",
            email=email,
        )

    # Create authorization code
    raw_code = await create_authorization_code(
        db,
        client_id=cid,
        user_id=user.user_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )

    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}code={raw_code}&state={state}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token_exchange(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")

        if not all([code, client_id, redirect_uri, code_verifier]):
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        try:
            cid = uuid.UUID(str(client_id))
        except ValueError:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Validate client
        client = await get_client(db, cid)
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Verify client_secret if provided
        if client_secret and not verify_client_secret(str(client_secret), client):
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Validate authorization code + PKCE
        auth_code = await validate_authorization_code(
            db,
            code=str(code),
            client_id=cid,
            redirect_uri=str(redirect_uri),
            code_verifier=str(code_verifier),
        )
        if auth_code is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Issue tokens
        tokens = await issue_tokens(
            db,
            client_id=cid,
            user_id=auth_code.user_id,
            scope=auth_code.scope,
        )
        return JSONResponse(tokens)

    elif grant_type == "refresh_token":
        refresh = form.get("refresh_token", "")
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")

        if not refresh or not client_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        try:
            cid = uuid.UUID(str(client_id))
        except ValueError:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Validate client
        client = await get_client(db, cid)
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        if client_secret and not verify_client_secret(str(client_secret), client):
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        tokens = await refresh_access_token(db, refresh_token=str(refresh), client_id=cid)
        if tokens is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse(tokens)

    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


@router.post("/oauth/revoke")
async def revoke(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    form = await request.form()
    token = form.get("token", "")
    token_type_hint = form.get("token_type_hint")

    if not token:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    await revoke_token(db, token=str(token), token_type_hint=str(token_type_hint) if token_type_hint else None)
    # RFC 7009: always return 200 even if token not found
    return Response(status_code=200)
```

**Step 2: Mount OAuth routes in app.py**

In `src/harbor_clerk/api/app.py`, add import:

```python
from harbor_clerk.api.routes.oauth import router as oauth_router
```

Add the router **before** the MCP mounts (before line 166) — note: OAuth routes are mounted without `/api` prefix since `/.well-known` must be at root:

```python
    app.include_router(oauth_router)
```

**Step 3: Lint and format**

Run: `cd /Users/alex/mcp-gateway && uv run ruff check src/harbor_clerk/api/routes/oauth.py src/harbor_clerk/api/app.py && uv run ruff format src/harbor_clerk/api/routes/oauth.py src/harbor_clerk/api/app.py`

**Step 4: Commit**

```bash
git add src/harbor_clerk/api/routes/oauth.py src/harbor_clerk/api/app.py
git commit -m "feat(oauth): add OAuth endpoints — register, authorize, token, revoke, well-known metadata"
```

---

### Task 7: Extend MCP middleware to accept OAuth tokens

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Test: `tests/test_mcp_auth.py` (append)

**Step 1: Write failing test for OAuth token in MCP auth**

Append to `tests/test_mcp_auth.py`:

```python
class TestMCPOAuthAuth:
    @pytest.mark.asyncio
    async def test_oauth_token_resolves_principal(self, monkeypatch):
        """OAuth access tokens should resolve to a user principal."""
        from harbor_clerk.mcp_server import _resolve_principal

        test_user_id = uuid.uuid4()

        async def mock_validate(db, token):
            if token == "oauth_test_token_abc":
                return test_user_id, "admin"
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server.validate_oauth_access_token", mock_validate)

        principal = await _resolve_principal("oauth_test_token_abc")
        assert principal is not None
        assert principal.type == "oauth"
        assert principal.id == test_user_id
        assert principal.role == "admin"

    @pytest.mark.asyncio
    async def test_oauth_token_invalid(self, monkeypatch):
        """Invalid OAuth tokens should fall through."""
        from harbor_clerk.mcp_server import _resolve_principal

        async def mock_validate(db, token):
            return None

        monkeypatch.setattr("harbor_clerk.mcp_server.validate_oauth_access_token", mock_validate)
        # Non-API-key, non-JWT token should try OAuth then return None
        # We need to also make JWT decoding fail
        monkeypatch.setattr("harbor_clerk.mcp_server.decode_token", lambda t: (_ for _ in ()).throw(Exception("bad")))

        principal = await _resolve_principal("not_a_valid_anything")
        assert principal is None
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_mcp_auth.py::TestMCPOAuthAuth -v`
Expected: FAIL — `validate_oauth_access_token` not found

**Step 3: Modify `_resolve_principal()` in `mcp_server.py`**

In `src/harbor_clerk/mcp_server.py`, update `_resolve_principal()` to try OAuth token validation as a fallback when JWT decoding fails. The function currently:
1. Checks if token is an API key (prefix check)
2. If not, tries JWT decode

Add a third path: if JWT fails, try OAuth token lookup.

Add import at top of file:

```python
from harbor_clerk.oauth import validate_access_token as validate_oauth_access_token
```

In `_resolve_principal()`, after the JWT decode fails (in the `except` block), add OAuth token lookup before returning `None`:

```python
    # Not an API key — try JWT first, then OAuth token
    try:
        payload = decode_token(token)
        # ... existing JWT logic ...
    except Exception:
        # JWT failed — try OAuth access token
        try:
            from harbor_clerk.db import async_session_factory
            async with async_session_factory() as db:
                result = await validate_oauth_access_token(db, token)
                if result is not None:
                    user_id, role = result
                    return Principal(type="oauth", id=user_id, role=role)
        except Exception:
            logger.debug("OAuth token validation failed", exc_info=True)
        return None
```

**Step 4: Run tests**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_mcp_auth.py -v`
Expected: All tests PASS (including new OAuth tests)

**Step 5: Lint**

Run: `cd /Users/alex/mcp-gateway && uv run ruff check src/harbor_clerk/mcp_server.py && uv run ruff format src/harbor_clerk/mcp_server.py`

**Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_auth.py
git commit -m "feat(oauth): extend MCP auth middleware to accept OAuth access tokens"
```

---

### Task 8: Add OAuth management API routes (for Integrations page)

**Files:**
- Modify: `src/harbor_clerk/api/routes/oauth.py`
- Modify: `src/harbor_clerk/api/app.py`

**Step 1: Add management endpoints**

Append to `src/harbor_clerk/api/routes/oauth.py`:

```python
# ---------------------------------------------------------------------------
# Management endpoints (admin-only, used by Integrations page)
# ---------------------------------------------------------------------------

from harbor_clerk.api.deps import require_admin, Principal


@router.get("/api/integrations/connections")
async def list_connections(
    _: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    """List active OAuth client connections."""
    from sqlalchemy import func, select

    result = await db.execute(
        select(
            OAuthClient.client_id,
            OAuthClient.client_name,
            OAuthClient.client_uri,
            OAuthClient.created_at,
            func.max(OAuthToken.last_used_at).label("last_used_at"),
            func.bool_or(
                (OAuthToken.revoked.is_(False))
                & (OAuthToken.refresh_token_expires_at > datetime.now(UTC))
            ).label("is_active"),
        )
        .outerjoin(OAuthToken, OAuthClient.client_id == OAuthToken.client_id)
        .group_by(OAuthClient.client_id)
        .order_by(OAuthClient.created_at.desc())
    )
    rows = result.all()

    return [
        {
            "client_id": str(row.client_id),
            "client_name": row.client_name,
            "client_uri": row.client_uri,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "is_active": bool(row.is_active),
        }
        for row in rows
    ]


from harbor_clerk.models.oauth_client import OAuthClient
from harbor_clerk.models.oauth_token import OAuthToken


@router.delete("/api/integrations/connections/{client_id}")
async def revoke_connection(
    client_id: uuid.UUID,
    _: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    """Revoke all tokens for a client and delete it."""
    from sqlalchemy import delete, update

    # Revoke all tokens
    await db.execute(
        update(OAuthToken).where(OAuthToken.client_id == client_id).values(revoked=True)
    )
    # Delete codes
    from harbor_clerk.models.oauth_code import OAuthCode

    await db.execute(delete(OAuthCode).where(OAuthCode.client_id == client_id))
    # Delete client
    await db.execute(delete(OAuthClient).where(OAuthClient.client_id == client_id))
    await db.commit()
    return {"ok": True}


@router.get("/api/integrations/settings")
async def get_integration_settings(
    _: Principal = Depends(require_admin),
):
    """Get OAuth-related settings."""
    settings = get_settings()
    return {
        "public_url": settings.public_url,
        "oauth_refresh_token_days": settings.oauth_refresh_token_days,
    }


@router.put("/api/integrations/settings")
async def update_integration_settings(
    request: Request,
    _: Principal = Depends(require_admin),
):
    """Update OAuth-related settings. Persists to config.json (native) or returns instruction for env vars (Docker)."""
    body = await request.json()
    settings = get_settings()

    public_url = body.get("public_url")
    refresh_days = body.get("oauth_refresh_token_days")

    # Update in-memory settings
    if public_url is not None:
        settings.public_url = public_url.rstrip("/")
    if refresh_days is not None:
        if refresh_days not in (30, 60, 90, 120, 365):
            raise HTTPException(400, "Invalid refresh token lifetime")
        settings.oauth_refresh_token_days = refresh_days

    # Persist to config file if native (NATIVE_CONFIG_FILE set)
    import json
    import os

    config_path = os.environ.get("NATIVE_CONFIG_FILE")
    if config_path:
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}
        if public_url is not None:
            config["public_url"] = settings.public_url
        if refresh_days is not None:
            config["oauth_refresh_token_days"] = refresh_days
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

    return {"ok": True}
```

**Step 2: Lint**

Run: `cd /Users/alex/mcp-gateway && uv run ruff check src/harbor_clerk/api/routes/oauth.py && uv run ruff format src/harbor_clerk/api/routes/oauth.py`

**Step 3: Commit**

```bash
git add src/harbor_clerk/api/routes/oauth.py
git commit -m "feat(oauth): add management API for integrations page — list, revoke, settings"
```

---

### Task 9: Build Integrations frontend page

**Files:**
- Create: `frontend/src/pages/IntegrationsPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Layout.tsx`

**Step 1: Create IntegrationsPage**

Create `frontend/src/pages/IntegrationsPage.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { get, del, put } from '../api'

interface Connection {
  client_id: string
  client_name: string | null
  client_uri: string | null
  created_at: string | null
  last_used_at: string | null
  is_active: boolean
}

interface IntegrationSettings {
  public_url: string
  oauth_refresh_token_days: number
}

const REFRESH_OPTIONS = [30, 60, 90, 120, 365]

export default function IntegrationsPage() {
  const [connections, setConnections] = useState<Connection[]>([])
  const [settings, setSettings] = useState<IntegrationSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [editUrl, setEditUrl] = useState('')
  const [saving, setSaving] = useState(false)

  async function load() {
    try {
      const [conns, s] = await Promise.all([
        get<Connection[]>('/api/integrations/connections'),
        get<IntegrationSettings>('/api/integrations/settings'),
      ])
      setConnections(conns)
      setSettings(s)
      setEditUrl(s.public_url)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function handleRevoke(clientId: string) {
    await del(`/api/integrations/connections/${clientId}`)
    setConnections((prev) => prev.filter((c) => c.client_id !== clientId))
  }

  async function handleSaveSettings() {
    if (!settings) return
    setSaving(true)
    try {
      await put('/api/integrations/settings', {
        public_url: editUrl,
        oauth_refresh_token_days: settings.oauth_refresh_token_days,
      })
      setSettings((s) => (s ? { ...s, public_url: editUrl } : s))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="text-sm text-gray-400">Loading...</div>

  return (
    <div className="space-y-8">
      <h1 className="text-xl font-bold">Integrations</h1>

      {/* Settings */}
      <section className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6">
        <h2 className="text-base font-semibold mb-4">Connection Settings</h2>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-600 dark:text-gray-400 mb-1">
              Public URL
            </label>
            <p className="text-xs text-gray-400 mb-2">
              Your Harbor Clerk instance must be accessible at this HTTPS URL for external AI tools to connect.
            </p>
            <div className="flex gap-2">
              <input
                type="url"
                value={editUrl}
                onChange={(e) => setEditUrl(e.target.value)}
                placeholder="https://harbor.example.com"
                className="flex-1 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm"
              />
              <button
                onClick={handleSaveSettings}
                disabled={saving || editUrl === settings?.public_url}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
              >
                Save
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-600 dark:text-gray-400 mb-1">
              Token lifetime
            </label>
            <select
              value={settings?.oauth_refresh_token_days ?? 90}
              onChange={async (e) => {
                const days = Number(e.target.value)
                setSettings((s) => (s ? { ...s, oauth_refresh_token_days: days } : s))
                await put('/api/integrations/settings', {
                  public_url: editUrl,
                  oauth_refresh_token_days: days,
                })
              }}
              className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) py-2 pl-3 pr-8 text-sm"
            >
              {REFRESH_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  {d} days
                </option>
              ))}
            </select>
          </div>
        </div>
      </section>

      {/* Active Connections */}
      <section className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6">
        <h2 className="text-base font-semibold mb-4">Active Connections</h2>
        {connections.length === 0 ? (
          <p className="text-sm text-gray-400">No external AI tools connected yet.</p>
        ) : (
          <div className="space-y-3">
            {connections.map((c) => (
              <div
                key={c.client_id}
                className="flex items-center justify-between rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) px-4 py-3"
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`h-2 w-2 rounded-full ${c.is_active ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                    />
                    <span className="text-sm font-medium">{c.client_name || 'Unknown client'}</span>
                  </div>
                  <div className="mt-1 text-xs text-gray-400">
                    {c.last_used_at ? `Last used ${new Date(c.last_used_at).toLocaleDateString()}` : 'Never used'}
                    {c.created_at && ` · Connected ${new Date(c.created_at).toLocaleDateString()}`}
                  </div>
                </div>
                <button
                  onClick={() => handleRevoke(c.client_id)}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Connect ChatGPT */}
      <section className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6">
        <h2 className="text-base font-semibold mb-1">Connect ChatGPT</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Use your ChatGPT Plus/Pro subscription to query your Harbor Clerk knowledge base.
        </p>
        <ol className="list-decimal list-inside space-y-2 text-sm text-gray-700 dark:text-gray-300">
          <li>
            Make Harbor Clerk accessible at a public HTTPS URL (e.g., via{' '}
            <a
              href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 dark:text-blue-400 underline"
            >
              Cloudflare Tunnel
            </a>
            )
          </li>
          <li>Enter your public URL in the settings above</li>
          <li>
            In ChatGPT, go to <strong>Settings → Connectors → Add MCP Server</strong>
          </li>
          <li>
            Paste your MCP URL:{' '}
            <code className="rounded bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 text-xs">
              {settings?.public_url ? `${settings.public_url}/mcp` : 'https://your-domain.com/mcp'}
            </code>
          </li>
          <li>ChatGPT will open a login window — sign in with your Harbor Clerk admin credentials</li>
          <li>Click <strong>Authorize</strong></li>
        </ol>
      </section>

      {/* Connect Claude */}
      <section className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6">
        <h2 className="text-base font-semibold mb-1">Connect Claude Desktop / Claude Code</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Claude Desktop and Claude Code can connect using an API key. Create one in{' '}
          <a href="/admin/keys" className="text-blue-600 dark:text-blue-400 underline">
            API Keys
          </a>
          , then:
        </p>
        <div className="space-y-4">
          <div>
            <h3 className="text-sm font-medium mb-2">Claude Desktop</h3>
            <p className="text-xs text-gray-400 mb-1">
              Add to <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">claude_desktop_config.json</code>:
            </p>
            <pre className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 text-xs overflow-x-auto">
              {JSON.stringify(
                {
                  mcpServers: {
                    'harbor-clerk': {
                      url: settings?.public_url ? `${settings.public_url}/mcp` : 'https://your-domain.com/mcp',
                      headers: { Authorization: 'Bearer YOUR_API_KEY' },
                    },
                  },
                },
                null,
                2,
              )}
            </pre>
          </div>
          <div>
            <h3 className="text-sm font-medium mb-2">Claude Code</h3>
            <pre className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 text-xs overflow-x-auto">
              claude mcp add harbor-clerk --transport http {settings?.public_url ? `${settings.public_url}/mcp` : 'https://your-domain.com/mcp'} --header
              &quot;Authorization: Bearer YOUR_API_KEY&quot;
            </pre>
          </div>
        </div>
      </section>

      {/* Connect Gemini */}
      <section className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6">
        <h2 className="text-base font-semibold mb-1">Connect Gemini CLI</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Gemini CLI can connect using an API key. Create one in{' '}
          <a href="/admin/keys" className="text-blue-600 dark:text-blue-400 underline">
            API Keys
          </a>
          , then add to your <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">settings.json</code>:
        </p>
        <pre className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 text-xs overflow-x-auto">
          {JSON.stringify(
            {
              mcpServers: {
                'harbor-clerk': {
                  uri: settings?.public_url ? `${settings.public_url}/mcp` : 'https://your-domain.com/mcp',
                  headers: { Authorization: 'Bearer YOUR_API_KEY' },
                },
              },
            },
            null,
            2,
          )}
        </pre>
      </section>
    </div>
  )
}
```

**Step 2: Add route in App.tsx**

In `frontend/src/App.tsx`, add import:

```typescript
import IntegrationsPage from './pages/IntegrationsPage'
```

Add route inside the `<AdminRoute>` block (after the `/admin/retrieval` route):

```tsx
<Route path="/integrations" element={<IntegrationsPage />} />
```

**Step 3: Add nav tab in Layout.tsx**

In `frontend/src/components/Layout.tsx`, add the Integrations tab next to System Settings (line 78):

```tsx
{isAdmin && <TabLink to="/integrations">Integrations</TabLink>}
{isAdmin && <TabLink to="/admin">System Settings</TabLink>}
```

**Step 4: Add `del` and `put` API helpers if missing**

Check if `frontend/src/api.ts` already has `del` and `put` exports. If not, add them following the same pattern as the existing `get` and `post` functions.

**Step 5: Lint and type-check**

Run: `cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check`

**Step 6: Commit**

```bash
git add frontend/src/pages/IntegrationsPage.tsx frontend/src/App.tsx frontend/src/components/Layout.tsx
git commit -m "feat(oauth): add Integrations page with connection management and setup guides"
```

---

### Task 10: Add comprehensive OAuth flow tests

**Files:**
- Modify: `tests/test_oauth.py`

**Step 1: Add PKCE verification tests**

Append to `tests/test_oauth.py`:

```python
import base64


class TestPKCE:
    def test_s256_verification_valid(self):
        from harbor_clerk.oauth import _verify_code_challenge

        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        # Compute expected challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert _verify_code_challenge(verifier, challenge, "S256") is True

    def test_s256_verification_invalid(self):
        from harbor_clerk.oauth import _verify_code_challenge

        assert _verify_code_challenge("wrong", "challenge", "S256") is False

    def test_plain_method_rejected(self):
        from harbor_clerk.oauth import _verify_code_challenge

        assert _verify_code_challenge("verifier", "verifier", "plain") is False


class TestTokenGeneration:
    def test_tokens_are_unique(self):
        from harbor_clerk.oauth import generate_token

        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_client_secrets_are_unique(self):
        from harbor_clerk.oauth import generate_client_secret

        secrets = {generate_client_secret() for _ in range(100)}
        assert len(secrets) == 100

    def test_hash_is_deterministic(self):
        from harbor_clerk.oauth import hash_token

        assert hash_token("abc") == hash_token("abc")
        assert hash_token("abc") != hash_token("def")
```

**Step 2: Run all OAuth tests**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/test_oauth.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_oauth.py
git commit -m "test(oauth): add PKCE verification and token generation tests"
```

---

### Task 11: Verify full stack

**Step 1: Lint all changed Python files**

Run: `cd /Users/alex/mcp-gateway && uv run ruff check src/harbor_clerk/oauth.py src/harbor_clerk/api/routes/oauth.py src/harbor_clerk/config.py src/harbor_clerk/mcp_server.py src/harbor_clerk/models/oauth_client.py src/harbor_clerk/models/oauth_code.py src/harbor_clerk/models/oauth_token.py && uv run ruff format --check src/harbor_clerk/`

**Step 2: Run all Python tests**

Run: `cd /Users/alex/mcp-gateway && uv run pytest tests/ -v`

**Step 3: Lint and type-check frontend**

Run: `cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check && npm run format:check`

**Step 4: Build frontend**

Run: `cd /Users/alex/mcp-gateway/frontend && npm run build`

**Step 5: Fix any issues found**

Address any lint, type, or test failures.

**Step 6: Final commit if fixes were needed**

```bash
git add -A
git commit -m "fix: address lint and type-check issues from OAuth implementation"
```

---

## Verification Checklist

After implementation, manually test:

1. **Metadata endpoints**: `curl https://your-url/.well-known/oauth-protected-resource` returns resource + auth server
2. **Client registration**: `curl -X POST https://your-url/oauth/register -d '{"redirect_uris":["https://example.com/cb"]}'` returns client_id + secret
3. **Authorization page**: Visit `https://your-url/oauth/authorize?response_type=code&client_id=<id>&redirect_uri=<uri>&code_challenge=<challenge>&code_challenge_method=S256` — shows login + consent page
4. **Token exchange**: POST to `/oauth/token` with authorization code + PKCE verifier — returns access + refresh tokens
5. **MCP with OAuth token**: Use access token as `Authorization: Bearer <token>` on `POST /mcp` — should work
6. **Refresh**: POST to `/oauth/token` with `grant_type=refresh_token` — returns new tokens
7. **Integrations page**: Visit `/integrations` — shows settings, connections, setup guides
8. **Revoke**: Click revoke on a connection — token stops working
9. **ChatGPT end-to-end**: Connect ChatGPT to Harbor Clerk via MCP (requires public URL + tunnel)
