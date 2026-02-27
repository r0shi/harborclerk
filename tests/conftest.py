"""
Test fixtures for Harbor Clerk.

Setup (one-time):
    docker compose up -d postgres
    docker compose exec postgres psql -U lka -c "CREATE DATABASE lka_test OWNER lka;"

Run:
    uv run pytest tests/ -v
"""

import os

# Must be set BEFORE any harbor_clerk import — db.py reads settings at module level.
# Respect DATABASE_URL if already set (e.g. macOS native server on port 5433).
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = (
        "postgresql+asyncpg://lka:lka_dev_password@localhost:5432/lka_test"
    )
os.environ["STORAGE_BACKEND"] = "filesystem"
os.environ["STORAGE_PATH"] = "/tmp/harbor_clerk_test_storage"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["STATIC_DIR"] = "/tmp/harbor_clerk_test_static"

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from harbor_clerk.config import get_settings
from harbor_clerk.models import Base, User
from harbor_clerk.models.enums import UserRole
from harbor_clerk.auth import create_access_token, hash_password


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def _engine():
    """Create engine and run Alembic migrations once per test session."""
    import subprocess

    settings = get_settings()

    # Run Alembic via subprocess to avoid nested asyncio.run() conflict.
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": settings.database_url},
    )

    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    yield engine
    # Session-scoped: engine is disposed when the process exits.


@pytest.fixture
async def db_session(
    _engine,
) -> AsyncGenerator[AsyncSession, None]:
    """Per-test session with table cleanup for isolation."""
    factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        yield session

    # Clean up all test data after each test
    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest.fixture
async def client(db_session: AsyncSession):
    """ASGI test client with DB session override."""
    from httpx import ASGITransport, AsyncClient
    from harbor_clerk.api.app import create_app
    from harbor_clerk.db import get_session

    app = create_app()

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user (flushed, not committed)."""
    user = User(
        email="admin@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
async def regular_user(db_session: AsyncSession) -> User:
    """Create a regular user (flushed, not committed)."""
    user = User(
        email="user@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
def admin_token(admin_user: User) -> str:
    return create_access_token(admin_user.user_id, admin_user.role.value)


@pytest.fixture
def user_token(regular_user: User) -> str:
    return create_access_token(regular_user.user_id, regular_user.role.value)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
