"""Tests for Alembic migration round-trips."""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

# Sync URL for Alembic (which runs its own event loop)
_SYNC_URL = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")


@pytest.fixture(scope="module")
def alembic_cfg():
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    return cfg


@pytest.fixture(scope="module")
def sync_engine():
    engine = create_engine(_SYNC_URL)
    yield engine
    engine.dispose()


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_upgrade_to_head(alembic_cfg, sync_engine):
    """Upgrade from current state to head; verify key tables exist."""
    command.upgrade(alembic_cfg, "head")
    tables = _table_names(sync_engine)
    for expected in ("users", "api_keys", "documents", "document_versions",
                     "document_pages", "chunks", "ingestion_jobs", "uploads",
                     "audit_log"):
        assert expected in tables, f"Table {expected} missing after upgrade"


def test_downgrade_to_base(alembic_cfg, sync_engine):
    """Downgrade to base; verify app tables are removed."""
    command.downgrade(alembic_cfg, "base")
    tables = _table_names(sync_engine)
    for table in ("users", "documents", "chunks"):
        assert table not in tables, f"Table {table} still present after downgrade"


def test_stepwise_upgrade(alembic_cfg, sync_engine):
    """Upgrade one revision at a time from base to head."""
    # Start from base
    command.downgrade(alembic_cfg, "base")
    assert "users" not in _table_names(sync_engine)

    revisions = ["0001", "0002", "0003", "0004", "0005"]
    for rev in revisions:
        command.upgrade(alembic_cfg, rev)

    tables = _table_names(sync_engine)
    assert "users" in tables
    assert "chunks" in tables


def test_re_upgrade_after_downgrade(alembic_cfg, sync_engine):
    """Full round-trip: head → base → head."""
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    tables = _table_names(sync_engine)
    assert "users" in tables
    assert "chunks" in tables
