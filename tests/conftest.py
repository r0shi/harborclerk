"""
Test fixtures for Harbor Clerk.

Setup:
    The test database is auto-created on whichever PostgreSQL is running.
    Detected ports in order: 5433 (macOS app), 5432 (Docker/local).

    Run tests with:
        uv run pytest tests/ -v

    To force a specific DB:
        DATABASE_URL=postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test uv run pytest

SAFETY: tests refuse to run against any database whose name isn't a recognized
test DB (must end in '_test' or be one of: harbor_clerk_test, lka_test).
This prevents accidental schema changes or data wipes on the live application
database. The db_session fixture truncates all tables between tests — running
it against a live DB would destroy all data.
"""

import os
import re
import sys

# Recognized test database names (exact match or *_test suffix)
_ALLOWED_TEST_DB_NAMES = {"harbor_clerk_test", "lka_test"}
_TEST_DB_SUFFIX = "_test"

# Default test DB name
_DEFAULT_TEST_DB = "harbor_clerk_test"


def _detect_postgres_port() -> int:
    """Find which PostgreSQL instance is running. Prefer 5433 (macOS app), fall back to 5432."""
    import socket

    for port in (5433, 5432):
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return port
        except OSError:
            continue
    # Nothing running — default to 5432 (CI uses this)
    return 5432


def _ensure_test_db_exists(port: int, dbname: str) -> None:
    """Create the test database if it doesn't exist. Uses asyncpg directly."""
    import asyncio

    import asyncpg

    async def _go() -> None:
        try:
            conn = await asyncpg.connect(
                host="localhost",
                port=port,
                user="lka",
                password="lka_dev_password",
                database="postgres",
            )
        except asyncpg.InvalidPasswordError:
            # macOS app PostgreSQL has no password for the lka user
            conn = await asyncpg.connect(host="localhost", port=port, user="lka", database="postgres")
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{dbname}" OWNER lka')
        finally:
            await conn.close()

    try:
        asyncio.run(_go())
    except Exception as e:
        # Non-fatal — if creation fails, the test run will surface a clearer error later
        print(f"Warning: could not ensure test DB '{dbname}' exists: {e}", file=sys.stderr)


def _parse_db_name(url: str) -> str:
    """Extract the database name from a PostgreSQL URL."""
    # postgresql+asyncpg://user:pass@host:port/dbname?params
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    return match.group(1) if match else ""


# Must be set BEFORE any harbor_clerk import — db.py reads settings at module level.
if "DATABASE_URL" in os.environ:
    # User-provided — verify it's a test DB
    _user_url = os.environ["DATABASE_URL"]
    _db_name = _parse_db_name(_user_url)
    if _db_name not in _ALLOWED_TEST_DB_NAMES and not _db_name.endswith(_TEST_DB_SUFFIX):
        print(
            f"\n\033[1;31mREFUSING TO RUN TESTS AGAINST DATABASE '{_db_name}'\033[0m\n"
            f"\n  DATABASE_URL = {_user_url}\n"
            "\n  Tests truncate all tables. Running against a live database will DESTROY DATA.\n"
            f"  Allowed test DB names: {sorted(_ALLOWED_TEST_DB_NAMES)} or any name ending in '{_TEST_DB_SUFFIX}'\n"
            "\n  To fix: unset DATABASE_URL (tests will auto-detect a running PG and use\n"
            f"  '{_DEFAULT_TEST_DB}'), or point DATABASE_URL at a test database.\n",
            file=sys.stderr,
        )
        sys.exit(2)
else:
    # Auto-detect a running PostgreSQL and use harbor_clerk_test on it
    _port = _detect_postgres_port()
    _ensure_test_db_exists(_port, _DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://lka:lka_dev_password@localhost:{_port}/{_DEFAULT_TEST_DB}"

os.environ["STORAGE_BACKEND"] = "filesystem"
os.environ["STORAGE_PATH"] = "/tmp/harbor_clerk_test_storage"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["STATIC_DIR"] = "/tmp/harbor_clerk_test_static"

# ruff: noqa: E402 — imports below MUST come after env setup (db.py reads settings at import time)
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from harbor_clerk.auth import create_access_token, hash_password
from harbor_clerk.config import get_settings
from harbor_clerk.models import Base, User
from harbor_clerk.models.enums import UserRole


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_global_settings_singleton():
    """Reset harbor_clerk.config._settings before AND after every test.

    The Settings object is a module-cached singleton (see config.get_settings).
    Several tests legitimately mutate it — setting ENABLE_CLI_ACCESS via
    monkeypatch.setenv and forcing a re-read with `_config._settings = None`,
    or directly mutating attributes like `s.allow_source_download = True` —
    and `monkeypatch` restores env vars on teardown but cannot rewind the
    in-memory attribute state of the cached instance. This left leaked values
    (e.g. enable_cli_access=True after tests/cli/test_e2e.py's happy-path test)
    visible to later test files in CI's alphabetical collection order.

    Resetting before-and-after every test makes the leak impossible regardless
    of which test mutates state or how. Negligible perf cost (one `None`
    assignment + lazy re-read from env on next `get_settings()`).
    """
    import harbor_clerk.config as _config

    _config._settings = None
    yield
    _config._settings = None


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

    # Clean up all test data after each test. schema_metadata is skipped —
    # its rows are migration-seeded sentinel data (embed_model/embed_dim/
    # reranker), not per-test data, and verify_schema_sentinel() relies on
    # them surviving across tests.
    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "schema_metadata":
                continue
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
