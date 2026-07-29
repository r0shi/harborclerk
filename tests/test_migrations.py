"""Tests for Alembic migration round-trips."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

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
    for expected in (
        "users",
        "api_keys",
        "documents",
        # document_versions was dropped by migration 0017 (flat document model)
        "document_pages",
        "document_headings",
        "chunks",
        "entities",
        "ingestion_jobs",
        "uploads",
        "upload_sessions",
        "audit_log",
        "conversations",
        "chat_messages",
    ):
        assert expected in tables, f"Table {expected} missing after upgrade"
    assert "document_versions" not in tables, "document_versions should be absent after 0017"


def test_migrating_does_not_disable_application_loggers(alembic_cfg, sync_engine):
    """`fileConfig` must not switch off every logger it doesn't name.

    alembic/env.py calls `logging.config.fileConfig`, whose `disable_existing_loggers`
    defaults to True — so running a migration in-process disabled all of
    harbor_clerk.* for the rest of the session. Production never noticed
    (migrations run as a subprocess both from /system/run-migrations and from
    MigrationRunner.swift), but in this suite it silenced every later log
    assertion, which is the worst kind of failure: tests that pass by capturing
    nothing.
    """
    import logging

    logger = logging.getLogger("harbor_clerk.config")
    command.upgrade(alembic_cfg, "head")

    assert not logger.disabled, (
        "running a migration disabled harbor_clerk logging — any later test asserting "
        "on log output will silently capture nothing"
    )
