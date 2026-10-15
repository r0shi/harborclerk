"""Tests for Alembic migration round-trips."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

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


def _chunk_index_names(engine) -> set[str]:
    return {ix["name"] for ix in inspect(engine).get_indexes("chunks")}


def _fold_function_exists(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT to_regprocedure('public.hc_fold_whitespace(text)') IS NOT NULL")).scalar()


def test_chunk_text_ws_fold_migration_round_trips(alembic_cfg, sync_engine):
    """0008 (#819) goes down to the plain trigram index and back up to the folded one."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        names = _chunk_index_names(sync_engine)
        assert "ix_chunks_chunk_text_trgm" in names
        assert "ix_chunks_chunk_text_ws_trgm" not in names
        assert not _fold_function_exists(sync_engine)
    finally:
        command.upgrade(alembic_cfg, "head")
    names = _chunk_index_names(sync_engine)
    assert "ix_chunks_chunk_text_ws_trgm" in names
    assert "ix_chunks_chunk_text_trgm" not in names
    assert _fold_function_exists(sync_engine)


def _load_0008():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0008_chunk_text_ws_fold.py"
    spec = importlib.util.spec_from_file_location("_mig_0008_rt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_state(engine, name: str):
    """(indisvalid, indexdef) for public.<name>, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
            ),
            {"n": name},
        ).first()
    return tuple(row) if row else None


def _seed_invalid_index(engine, name: str) -> None:
    """An INVALID index of this name, as an interrupted CREATE INDEX CONCURRENTLY leaves (superuser test DB)."""
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX {name} ON public.chunks (chunk_num)"))
        conn.execute(text(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'public.{name}'::regclass"))


def test_0008_upgrade_rebuilds_an_invalid_leftover_index(alembic_cfg, sync_engine):
    """A retry after an interrupted build finds the function committed and an INVALID
    ix_chunks_chunk_text_ws_trgm; it must rebuild it, not skip it and drop the valid plain index."""
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        with sync_engine.begin() as conn:
            conn.execute(text(mig._FUNCTION_DDL.format(name=mig.FOLD_FUNCTION)))
        _seed_invalid_index(sync_engine, mig.FOLD_INDEX)
        assert _index_state(sync_engine, mig.FOLD_INDEX)[0] is False

        command.upgrade(alembic_cfg, "head")

        valid, indexdef = _index_state(sync_engine, mig.FOLD_INDEX)
        assert valid is True
        assert "hc_fold_whitespace(chunk_text)" in indexdef and "gin_trgm_ops" in indexdef
        assert _index_state(sync_engine, mig.PLAIN_INDEX) is None
    finally:
        command.upgrade(alembic_cfg, "head")


def test_0008_downgrade_rebuilds_an_invalid_leftover_index(alembic_cfg, sync_engine):
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        _seed_invalid_index(sync_engine, mig.PLAIN_INDEX)

        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")

        valid, indexdef = _index_state(sync_engine, mig.PLAIN_INDEX)
        assert valid is True
        assert "gin_trgm_ops" in indexdef and "(chunk_text gin_trgm_ops)" in indexdef
        assert _index_state(sync_engine, mig.FOLD_INDEX) is None
    finally:
        command.upgrade(alembic_cfg, "head")


def test_0008_refuses_to_drop_the_old_index_when_the_new_one_is_invalid(sync_engine):
    """The post-build check: an index left INVALID after the CREATE raises before anything is dropped."""
    mig = _load_0008()
    name = "ix_test_0008_left_invalid"
    ddl = (
        f"CREATE INDEX IF NOT EXISTS {name} ON public.chunks (chunk_num); "
        f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'public.{name}'::regclass"
    )
    with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            with pytest.raises(RuntimeError, match="INVALID"):
                mig._build_index_concurrently(conn, name, ddl)
        finally:
            conn.execute(text(f"DROP INDEX IF EXISTS public.{name}"))


def test_0008_refuses_a_fold_function_with_a_different_body(alembic_cfg, sync_engine):
    """Never redefine the function under an expression index: an existing, different body raises."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE FUNCTION public.hc_fold_whitespace(text) RETURNS text "
                    "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE RETURN lower($1)"
                )
            )
        with pytest.raises(RuntimeError, match="different definition"):
            command.upgrade(alembic_cfg, "head")
    finally:
        with sync_engine.begin() as conn:
            conn.execute(text("DROP FUNCTION IF EXISTS public.hc_fold_whitespace(text)"))
        command.upgrade(alembic_cfg, "head")
    assert _fold_function_exists(sync_engine)
