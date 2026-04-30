import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from harbor_clerk.config import get_settings
from harbor_clerk.models import Base


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="session")
def _sync_engine(_engine):
    """Sync engine bound to the same test DB. Depends on _engine to ensure migrations have run."""
    settings = get_settings()
    eng = create_engine(_sync_url(settings.database_url), echo=False)
    yield eng
    eng.dispose()


@pytest.fixture
def sync_session(_sync_engine):
    """Per-test sync session with table cleanup."""
    factory = sessionmaker(bind=_sync_engine, expire_on_commit=False)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        # Cleanup all tables (sync) — mirrors db_session's async cleanup
        with _sync_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
