"""Synchronous SQLAlchemy engine + session for RQ workers.

RQ workers are synchronous — this provides sync DB access via psycopg2
instead of wrapping everything in asyncio.run().
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from harbor_clerk.config import get_settings


def _make_sync_url(async_url: str) -> str:
    """Convert postgresql+asyncpg:// to postgresql+psycopg2://."""
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


_engine = None
_session_factory = None


def get_sync_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            _make_sync_url(settings.database_url),
            echo=False,
            pool_size=8,
            max_overflow=12,
            pool_pre_ping=True,
        )
    return _engine


def get_sync_session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
        )
    return _session_factory()
