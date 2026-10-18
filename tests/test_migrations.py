"""Tests for Alembic migration round-trips."""

import json
import os
import threading
import time

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


# The revision before 0011 (#828), which replaces 0008's function and index and resets 0009's columns:
# the 0008 and 0009 tests observe their own work there.
PRE_0011 = "0010_chat_message_seq"
HEAD_FOLD_INDEX = "ix_chunks_chunk_text_fold_trgm"


def _fold_function_exists(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT to_regprocedure('public.hc_fold_whitespace(text)') IS NOT NULL")).scalar()


def test_chunk_text_ws_fold_migration_round_trips(alembic_cfg, sync_engine):
    """0008 (#819) goes down to the plain trigram index and back up to the folded one (which 0011
    then replaces: observed at the revision before it)."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        names = _chunk_index_names(sync_engine)
        assert "ix_chunks_chunk_text_trgm" in names
        assert "ix_chunks_chunk_text_ws_trgm" not in names
        assert not _fold_function_exists(sync_engine)
        command.upgrade(alembic_cfg, PRE_0011)
        names = _chunk_index_names(sync_engine)
        assert "ix_chunks_chunk_text_ws_trgm" in names
        assert "ix_chunks_chunk_text_trgm" not in names
        assert _fold_function_exists(sync_engine)
    finally:
        command.upgrade(alembic_cfg, "head")


def _load_0008():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0008_chunk_text_ws_fold.py"
    spec = importlib.util.spec_from_file_location("_mig_0008_rt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0011():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0011_punctuation_fold.py"
    spec = importlib.util.spec_from_file_location("_mig_0011_rt", path)
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


def _drop_invalid_leftovers(engine, *names: str) -> None:
    """Cleanup that holds even when the migration under test regressed and left an INVALID index."""
    for name in names:
        state = _index_state(engine, name)
        if state is not None and state[0] is False:
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX IF EXISTS public.{name}"))


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

        command.upgrade(alembic_cfg, PRE_0011)

        valid, indexdef = _index_state(sync_engine, mig.FOLD_INDEX)
        assert valid is True
        assert "hc_fold_whitespace(chunk_text)" in indexdef and "gin_trgm_ops" in indexdef
        assert _index_state(sync_engine, mig.PLAIN_INDEX) is None
    finally:
        _drop_invalid_leftovers(sync_engine, mig.FOLD_INDEX, mig.PLAIN_INDEX)
        command.upgrade(alembic_cfg, "head")


def test_0008_downgrade_rebuilds_an_invalid_leftover_index(alembic_cfg, sync_engine):
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        # Seeded at 0008, where an interrupted 0008 downgrade leaves it: 0009's downgrade rewrites the
        # table, and a rewrite rebuilds every index, which would make a leftover seeded at head valid.
        command.downgrade(alembic_cfg, "0008_chunk_text_ws_fold")
        _seed_invalid_index(sync_engine, mig.PLAIN_INDEX)

        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")

        valid, indexdef = _index_state(sync_engine, mig.PLAIN_INDEX)
        assert valid is True
        assert "gin_trgm_ops" in indexdef and "(chunk_text gin_trgm_ops)" in indexdef
        assert _index_state(sync_engine, mig.FOLD_INDEX) is None
    finally:
        _drop_invalid_leftovers(sync_engine, mig.FOLD_INDEX, mig.PLAIN_INDEX)
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


def _variant_function_ddl(variant: str) -> str:
    mig = _load_0008()
    ddl = mig._FUNCTION_DDL.format(name=mig.FOLD_FUNCTION)
    if variant == "different body":
        return ddl.split(" RETURN ")[0] + " RETURN lower($1)"
    if variant == "set search_path":
        return ddl.replace(" RETURN ", " SET search_path = public RETURN ", 1)
    if variant == "security definer":
        return ddl.replace(" RETURN ", " SECURITY DEFINER RETURN ", 1)
    if variant == "parallel unsafe":
        return ddl.replace("PARALLEL SAFE", "PARALLEL UNSAFE", 1)
    raise AssertionError(variant)


@pytest.mark.parametrize("variant", ["different body", "set search_path", "security definer", "parallel unsafe"])
def test_0008_refuses_a_fold_function_with_a_different_definition(alembic_cfg, sync_engine, variant):
    """Never redefine the function under an expression index. A same-body function with SET options or
    SECURITY DEFINER stops inlining (the index no longer matches); PARALLEL UNSAFE forces serial plans."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        with sync_engine.begin() as conn:
            conn.execute(text(_variant_function_ddl(variant)))
        with pytest.raises(RuntimeError, match="different definition"):
            command.upgrade(alembic_cfg, "head")
    finally:
        # Robust even if the check regressed and an index was built on the wrong body: drop both,
        # restamp, rebuild from the real definition. If the regression let the run reach 0009, the
        # FTS columns depend on the function too, and CASCADE would drop them: reset them to
        # 0001's expressions first (a no-op rewrite at 0007).
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE public.chunks "
                    "ALTER COLUMN fts_en SET EXPRESSION AS (to_tsvector('english', coalesce(chunk_text, ''))), "
                    "ALTER COLUMN fts_fr SET EXPRESSION AS (to_tsvector('french', coalesce(chunk_text, '')))"
                )
            )
            conn.execute(text("DROP FUNCTION IF EXISTS public.hc_fold_whitespace(text) CASCADE"))
        command.stamp(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        command.upgrade(alembic_cfg, "head")
    # Healthy head: 0011 replaced 0008's function and index with its own.
    assert _index_state(sync_engine, HEAD_FOLD_INDEX)[0] is True


def _occupy_name(engine, name: str) -> None:
    """A non-index relation holding the index's name: CREATE INDEX IF NOT EXISTS skips it, so the
    build "succeeds" with no index, a real failure path through _require_valid without any patching."""
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SEQUENCE public.{name}"))


def _free_name(engine, name: str) -> None:
    """Drop the occupying sequence, and nothing else: the name may by now be a real index."""
    with engine.begin() as conn:
        kind = conn.execute(text("SELECT relkind FROM pg_class WHERE oid = to_regclass(:n)"), {"n": f"public.{name}"})
        if kind.scalar() == "S":
            conn.execute(text(f"DROP SEQUENCE public.{name}"))


def _restore_head(alembic_cfg, engine) -> None:
    """Back to a healthy head even if the migration under test regressed: free occupied names, drop
    INVALID leftovers, and if the folded index is still missing at head, restamp 0007 and rebuild."""
    mig = _load_0008()
    _free_name(engine, mig.FOLD_INDEX)
    _free_name(engine, mig.PLAIN_INDEX)
    _drop_invalid_leftovers(engine, mig.FOLD_INDEX, mig.PLAIN_INDEX)
    command.upgrade(alembic_cfg, "head")
    if _index_state(engine, HEAD_FOLD_INDEX) is None:
        command.stamp(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        command.upgrade(alembic_cfg, "head")


def test_0008_upgrade_keeps_the_old_index_when_the_new_build_fails(alembic_cfg, sync_engine):
    """The plain index is dropped only after the folded one is valid."""
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        _occupy_name(sync_engine, mig.FOLD_INDEX)
        with pytest.raises(RuntimeError, match="missing or INVALID"):
            command.upgrade(alembic_cfg, "head")
        assert _index_state(sync_engine, mig.PLAIN_INDEX)[0] is True, "old index dropped before the new one was valid"
    finally:
        _restore_head(alembic_cfg, sync_engine)


def test_0008_downgrade_keeps_the_folded_index_when_the_plain_build_fails(alembic_cfg, sync_engine):
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        _occupy_name(sync_engine, mig.PLAIN_INDEX)
        with pytest.raises(RuntimeError, match="missing or INVALID"):
            command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        assert _index_state(sync_engine, mig.FOLD_INDEX)[0] is True, (
            "folded index dropped before the plain one was valid"
        )
    finally:
        _restore_head(alembic_cfg, sync_engine)


def test_0008_does_not_drop_an_index_whose_build_is_still_running(sync_engine):
    """An orphaned build (client gone, backend still building) shows as INVALID; a retry must raise
    and name the pid, not drop the index and start over. A real CREATE INDEX CONCURRENTLY is held in
    its wait-for-writers phase by an open ROW EXCLUSIVE lock; every wait here is bounded."""
    mig = _load_0008()
    name = "ix_test_0008_in_progress"
    ddl = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON public.chunks (chunk_num)"
    errors: list[Exception] = []

    blocker = sync_engine.connect()
    blocker_tx = blocker.begin()
    blocker.execute(text("LOCK TABLE public.chunks IN ROW EXCLUSIVE MODE"))

    def _build():
        try:
            with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("SET statement_timeout = '30s'"))
                conn.execute(text(f"CREATE INDEX CONCURRENTLY {name} ON public.chunks (chunk_num)"))
        except Exception as exc:  # reported below, never swallowed
            errors.append(exc)

    builder = threading.Thread(target=_build, daemon=True)
    builder.start()
    try:
        with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Bounded: without the guard, DROP INDEX CONCURRENTLY would wait on the builder forever.
            conn.execute(text("SET lock_timeout = '3s'"))
            deadline = time.monotonic() + 10
            while (pid := mig._build_in_progress(conn, name)) is None:
                assert time.monotonic() < deadline, "the concurrent build never showed in pg_stat_progress_create_index"
                time.sleep(0.05)
            assert mig._index_valid(conn, name) is False
            with pytest.raises(RuntimeError, match=f"index build in progress \\(pid {pid}\\)"):
                mig._build_index_concurrently(conn, name, ddl)
            assert mig._index_valid(conn, name) is False, "the in-progress index was dropped"
    finally:
        blocker_tx.rollback()
        blocker.close()
        builder.join(30)
        with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP INDEX IF EXISTS public.{name}"))
    assert not builder.is_alive(), "the concurrent build did not finish after the lock was released"
    assert errors == []


def _fts_expressions(engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.attname, pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
                "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = 'public.chunks'::regclass AND a.attname IN ('fts_en', 'fts_fr')"
            )
        )
        return dict(rows.all())


def test_fts_over_folded_text_migration_round_trips(alembic_cfg, sync_engine):
    """0009 (#825) goes down to the unfolded expressions of 0001 and back up to the folded ones (which
    0011 then resets: observed at the revision before it)."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0008_chunk_text_ws_fold")
        assert _fts_expressions(sync_engine) == {
            "fts_en": "to_tsvector('english'::regconfig, COALESCE(chunk_text, ''::text))",
            "fts_fr": "to_tsvector('french'::regconfig, COALESCE(chunk_text, ''::text))",
        }
        command.upgrade(alembic_cfg, PRE_0011)
        assert _fts_expressions(sync_engine) == WS_FOLD_EXPRESSIONS
        names = _chunk_index_names(sync_engine)
        assert {"chunks_fts_en_idx", "chunks_fts_fr_idx"} <= names
    finally:
        command.upgrade(alembic_cfg, "head")


WS_FOLD_EXPRESSIONS = {
    "fts_en": "to_tsvector('english'::regconfig, hc_fold_whitespace(COALESCE(chunk_text, ''::text)))",
    "fts_fr": "to_tsvector('french'::regconfig, hc_fold_whitespace(COALESCE(chunk_text, ''::text)))",
}
SEARCH_FOLD_EXPRESSIONS = {
    "fts_en": "to_tsvector('english'::regconfig, hc_fold_search_text(COALESCE(chunk_text, ''::text)))",
    "fts_fr": "to_tsvector('french'::regconfig, hc_fold_search_text(COALESCE(chunk_text, ''::text)))",
}


def test_0009_refuses_a_fold_function_with_a_different_definition(alembic_cfg, sync_engine):
    """The stored columns are computed with the function's body of the day, like 0008's index:
    0009 builds only on 0008's exact definition."""
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0008_chunk_text_ws_fold")
        with sync_engine.begin() as conn:
            conn.execute(
                text(_variant_function_ddl("different body").replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
            )
        with pytest.raises(RuntimeError, match="different definition"):
            command.upgrade(alembic_cfg, "head")
        assert "hc_fold_whitespace" not in _fts_expressions(sync_engine)["fts_en"]
    finally:
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    mig._FUNCTION_DDL.format(name=mig.FOLD_FUNCTION).replace(
                        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"
                    )
                )
            )
            conn.execute(text(f"REINDEX INDEX public.{mig.FOLD_INDEX}"))
        command.upgrade(alembic_cfg, "head")
    assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS


@pytest.mark.parametrize(("migration", "revision"), [("0008", PRE_0011), ("0011", "head")])
def test_fold_guard_passes_twice_on_one_asyncpg_connection(alembic_cfg, sync_engine, migration, revision):
    """The guard resolves both functions when its statement runs: asyncpg caches prepared
    statements per connection, and a '...'::regprocedure literal resolved at prepare time made the
    second check compare against the dropped first twin and refuse an identical function. The
    function must already exist, or the first call creates it and never compares: 0008's exists
    up to 0010, 0011's from 0011."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    mig = _load_0008() if migration == "0008" else _load_0011()
    command.upgrade(alembic_cfg, "head")
    try:
        if revision != "head":
            command.downgrade(alembic_cfg, revision)
        assert _function_exists(sync_engine, mig.FOLD_FUNCTION.removeprefix("public."))

        async def _twice():
            engine = create_async_engine(os.environ["DATABASE_URL"])
            try:
                async with engine.connect() as conn:
                    for _ in range(2):
                        await conn.run_sync(mig._create_fold_function)
                    await conn.rollback()
            finally:
                await engine.dispose()

        asyncio.run(_twice())
    finally:
        command.upgrade(alembic_cfg, "head")


def test_upgrade_from_0007_with_the_fold_function_already_present(alembic_cfg, sync_engine):
    """0007 -> head in one run, with the function left behind by an interrupted earlier attempt
    (0008's autocommit block commits it): 0008 and then 0009 both check it on the same connection."""
    mig = _load_0008()
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0007_ingestion_job_pipeline_seq")
        with sync_engine.begin() as conn:
            conn.execute(text(mig._FUNCTION_DDL.format(name=mig.FOLD_FUNCTION)))
        command.upgrade(alembic_cfg, "head")
    finally:
        _restore_head(alembic_cfg, sync_engine)
    assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS
    assert _index_state(sync_engine, HEAD_FOLD_INDEX)[0] is True


def _activity(engine, name: str) -> tuple | None:
    """(state, wait_event_type, wait_event, query, backend_xmin) of the backend carrying ``name``."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state, wait_event_type, wait_event, query, backend_xmin::text FROM pg_stat_activity "
                "WHERE application_name = :name AND datname = current_database()"
            ),
            {"name": name},
        ).first()
    return tuple(row) if row else None


def _wait_until(predicate, what: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.1)


@pytest.mark.parametrize(
    ("start", "blocker_mode"),
    [
        # The first run waits on 0009's ALTER TABLE.
        ("0008_chunk_text_ws_fold", "ACCESS SHARE"),
        # The first run waits inside 0008's CREATE INDEX CONCURRENTLY, which later waits for every
        # older snapshot in the database: a second run blocked in pg_advisory_lock holds one, and
        # waits on the lock the first run holds, so the deadlock detector killed the first run
        # mid-build. A second run that polls with pg_try_advisory_lock outside a transaction does
        # not hold a snapshot while it waits.
        ("0007_ingestion_job_pipeline_seq", "ROW EXCLUSIVE"),
    ],
)
def test_concurrent_runs_are_serialised_and_rewrite_once(alembic_cfg, sync_engine, start, blocker_mode):
    """Two `alembic upgrade head` (a double click, a restart during a run): the second waits for the
    first, without holding a snapshot or a transaction, then finds head. Without the lock both
    queued on 0009's ALTER, rewrote chunks twice, and the second failed to stamp. The first run is
    held on a lock on chunks until the second is waiting; an event trigger counts the rewrites."""
    import subprocess

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, start)
    with sync_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS hc_test_rewrites (relid oid NOT NULL)"))
        conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION hc_test_log_rewrite() RETURNS event_trigger LANGUAGE plpgsql AS "
                "$$ BEGIN INSERT INTO hc_test_rewrites VALUES (pg_event_trigger_table_rewrite_oid()); END $$"
            )
        )
        conn.execute(
            text("CREATE EVENT TRIGGER hc_test_rewrite ON table_rewrite EXECUTE FUNCTION hc_test_log_rewrite()")
        )
    first, second = "hc-test-concurrent-first", "hc-test-concurrent-second"
    procs: list[subprocess.Popen] = []
    blocker = sync_engine.connect()
    blocker_tx = blocker.begin()

    def _start(name: str) -> None:
        procs.append(
            subprocess.Popen(
                ["alembic", "upgrade", "head"],
                env={**os.environ, "HC_MIGRATION_APPLICATION_NAME": name},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )

    try:
        blocker.execute(text(f"LOCK TABLE public.chunks IN {blocker_mode} MODE"))
        _start(first)
        _wait_until(lambda: (_activity(sync_engine, first) or (None, None))[1] == "Lock", "the first run to block")
        _start(second)

        def _second_waiting() -> bool:
            # Blocked in a lock (pg_advisory_lock, or the same ALTER without any lock), or polling:
            # idle between tries, its last statement the COMMIT that ends each try.
            act = _activity(sync_engine, second)
            return act is not None and (act[1] == "Lock" or (act[0] == "idle" and (act[3] or "").startswith("COMMIT")))

        _wait_until(_second_waiting, "the second run to wait")
        second_while_waiting = _activity(sync_engine, second)
        blocker_tx.rollback()
        outputs = [p.communicate(timeout=120)[0] for p in procs]
        assert [p.returncode for p in procs] == [0, 0], [out[-800:] for out in outputs]
        with sync_engine.connect() as conn:
            rewrites = conn.execute(
                text("SELECT count(*) FROM hc_test_rewrites WHERE relid = 'public.chunks'::regclass")
            ).scalar()
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rewrites == 2  # 0009 and 0011, once each
        assert version == "0011_punctuation_fold"
        assert _index_state(sync_engine, HEAD_FOLD_INDEX)[0] is True
        # The second run polled: idle between tries (no open transaction), no lock wait, no snapshot.
        state, wait_type, _, _, xmin = second_while_waiting
        assert state == "idle" and wait_type != "Lock" and xmin is None, second_while_waiting
        # ... and said who it was waiting for.
        assert "Waiting for another migration run to finish (held by pid " in outputs[1], outputs[1][-800:]
        assert f"application_name '{first}')" in outputs[1], outputs[1][-800:]
    finally:
        if blocker_tx.is_active:
            blocker_tx.rollback()
        blocker.close()
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        with sync_engine.begin() as conn:
            conn.execute(text("DROP EVENT TRIGGER IF EXISTS hc_test_rewrite"))
            conn.execute(text("DROP FUNCTION IF EXISTS hc_test_log_rewrite()"))
            conn.execute(text("DROP TABLE IF EXISTS hc_test_rewrites"))
        _drop_invalid_leftovers(sync_engine, "ix_chunks_chunk_text_ws_trgm", "ix_chunks_chunk_text_trgm")
        command.upgrade(alembic_cfg, "head")


def test_0009_fails_fast_when_chunks_is_held(alembic_cfg, sync_engine):
    """On a live API another transaction may hold chunks; 0009's ALTER waits at most 10 s
    (lock_timeout) rather than queueing every search behind it, says so, and leaves 0008 stamped."""
    import subprocess

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "0008_chunk_text_ws_fold")
    blocker = sync_engine.connect()
    blocker_tx = blocker.begin()
    try:
        blocker.execute(text("LOCK TABLE public.chunks IN ROW EXCLUSIVE MODE"))
        started = time.monotonic()
        # A subprocess with a bound: without the lock_timeout the ALTER would wait for the blocker
        # forever, and this test would hang instead of failing.
        run = subprocess.run(
            ["alembic", "upgrade", "head"], env=dict(os.environ), capture_output=True, text=True, timeout=60
        )
        elapsed = time.monotonic() - started
        assert run.returncode != 0
        assert "could not lock public.chunks within 10s: another transaction holds it" in run.stderr, run.stderr[-800:]
        blocker_tx.rollback()
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0008_chunk_text_ws_fold"
        assert "hc_fold_whitespace" not in _fts_expressions(sync_engine)["fts_en"]
        assert 9 < elapsed < 30, elapsed
    finally:
        if blocker_tx.is_active:
            blocker_tx.rollback()
        blocker.close()
        command.upgrade(alembic_cfg, "head")
    assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS


# ---------------------------------------------------------------------------
# 0010: chat_messages.seq (#809)
# ---------------------------------------------------------------------------

_SEQ_USER = "00000000-0000-0000-0000-00000000a809"
_SEQ_CONV_A = "00000000-0000-0000-0000-00000000c001"
_SEQ_CONV_B = "00000000-0000-0000-0000-00000000c002"


def _msg_id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _seed_chat_owner(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO users (user_id, email, password_hash, role) VALUES (:u, 'seq-migration@test.com', 'x', 'user')"
        ),
        {"u": _SEQ_USER},
    )
    for conv in (_SEQ_CONV_A, _SEQ_CONV_B):
        conn.execute(
            text("INSERT INTO conversations (conversation_id, user_id, title) VALUES (:c, :u, 'seq')"),
            {"c": conv, "u": _SEQ_USER},
        )


def _seed_unordered_chat(engine) -> int:
    """Rows written at 0009, without seq, in a physical order unlike the order chat_stream stored them in. Returns
    how many.

    Conversation A: its first question alone at T0 (the title commit), then a turn at T1 holding a call with two ids
    (the second id's result stored first), a second call and its result, a result that answers no call, and the
    answer; a second turn at T2. Conversation B ties with A's T1 turn. Message ids are fixed, so the backfill's
    message_id tie-break (between the calls of one turn) is known."""

    def calls(*ids: str) -> str:
        return json.dumps(
            [{"id": i, "type": "function", "function": {"name": "search_documents", "arguments": "{}"}} for i in ids]
        )

    t0, t1, t2 = "2026-10-01 10:00:00+00", "2026-10-01 10:00:05+00", "2026-10-01 10:01:00+00"
    # (message_id, conversation, role, content, tool_calls, tool_call_id, created_at), in insertion (physical) order.
    rows = [
        (_msg_id(19), _SEQ_CONV_A, "assistant", "answer 1", None, None, t1),
        (_msg_id(11), _SEQ_CONV_A, "tool", "orphan", None, "nobody", t1),
        (_msg_id(18), _SEQ_CONV_A, "tool", "result b_1", None, "b_1", t1),
        (_msg_id(13), _SEQ_CONV_A, "tool", "result a_2", None, "a_2", t1),
        (_msg_id(12), _SEQ_CONV_A, "assistant", "", calls("b_1"), None, t1),
        (_msg_id(14), _SEQ_CONV_A, "tool", "result a_1", None, "a_1", t1),
        (_msg_id(10), _SEQ_CONV_A, "assistant", "", calls("a_1", "a_2"), None, t1),
        (_msg_id(21), _SEQ_CONV_A, "assistant", "answer 2", None, None, t2),
        (_msg_id(20), _SEQ_CONV_A, "user", "question 2", None, None, t2),
        (_msg_id(1), _SEQ_CONV_A, "user", "question 1", None, None, t0),
        (_msg_id(31), _SEQ_CONV_B, "assistant", "answer b", None, None, t1),
        (_msg_id(30), _SEQ_CONV_B, "user", "question b", None, None, t1),
    ]
    with engine.begin() as conn:
        _seed_chat_owner(conn)
        for message_id, conv, role, content, tool_calls, tool_call_id, created_at in rows:
            conn.execute(
                text(
                    "INSERT INTO chat_messages (message_id, conversation_id, role, content, tool_calls, tool_call_id, "
                    "created_at) VALUES (:m, :c, :r, :content, CAST(:tc AS jsonb), :tcid, CAST(:at AS timestamptz))"
                ),
                {
                    "m": message_id,
                    "c": conv,
                    "r": role,
                    "content": content,
                    "tc": tool_calls,
                    "tcid": tool_call_id,
                    "at": created_at,
                },
            )
    return len(rows)


def _delete_seeded_chat(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM conversations WHERE user_id = :u"), {"u": _SEQ_USER})
        conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": _SEQ_USER})


def _contents_by_seq(engine, conv: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT content, role FROM chat_messages WHERE conversation_id = :c ORDER BY seq"), {"c": conv}
        )
        return [content or role for content, role in rows]


def _chat_columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("chat_messages")}


def test_0010_backfills_seq_and_round_trips(alembic_cfg, sync_engine):
    """Upgrade numbers existing rows in the stated best-effort order (question, each call followed by its results in
    the order of its ids, unmatched results, answer), continues the identity above them; downgrade drops it."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0009_fts_over_folded_text")
        with sync_engine.connect() as conn:
            existing = conn.execute(text("SELECT count(*) FROM chat_messages")).scalar()
        seeded = _seed_unordered_chat(sync_engine)

        command.upgrade(alembic_cfg, "head")

        assert _contents_by_seq(sync_engine, _SEQ_CONV_A) == [
            "question 1",
            "assistant",  # the call with ids a_1, a_2 (message id 10, before 12)
            "result a_1",
            "result a_2",
            "assistant",  # the call b_1
            "result b_1",
            "orphan",
            "answer 1",
            "question 2",
            "answer 2",
        ]
        assert _contents_by_seq(sync_engine, _SEQ_CONV_B) == ["question b", "answer b"]
        with sync_engine.connect() as conn:
            seqs = conn.execute(text("SELECT seq FROM chat_messages ORDER BY seq")).scalars().all()
            identity = conn.execute(
                text(
                    "SELECT attidentity, attnotnull FROM pg_attribute "
                    "WHERE attrelid = 'public.chat_messages'::regclass AND attname = 'seq'"
                )
            ).one()
        total = existing + seeded
        assert seqs == list(range(1, total + 1))
        assert tuple(identity) == ("a", True)
        assert "(conversation_id, created_at, seq)" in _index_state(sync_engine, "idx_messages_conv")[1]
        with sync_engine.begin() as conn:
            new_seq = conn.execute(
                text("INSERT INTO chat_messages (conversation_id, role) VALUES (:c, 'user') RETURNING seq"),
                {"c": _SEQ_CONV_A},
            ).scalar()
        assert new_seq == total + 1

        command.downgrade(alembic_cfg, "0009_fts_over_folded_text")

        assert "seq" not in _chat_columns(sync_engine)
        assert "(conversation_id, created_at)" in _index_state(sync_engine, "idx_messages_conv")[1]
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM chat_messages")).scalar() == total + 1
    finally:
        _delete_seeded_chat(sync_engine)
        command.upgrade(alembic_cfg, "head")
    assert "seq" in _chat_columns(sync_engine)


def test_0010_upgrade_of_an_empty_table_starts_seq_at_one(alembic_cfg, sync_engine):
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, "0009_fts_over_folded_text")
        # Its own empty table, whatever earlier tests left (a test database: db_session's cleanup empties it too).
        with sync_engine.begin() as conn:
            conn.execute(text("DELETE FROM chat_messages"))
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM chat_messages")).scalar() == 0
        command.upgrade(alembic_cfg, "head")
        with sync_engine.begin() as conn:
            _seed_chat_owner(conn)
            first = conn.execute(
                text("INSERT INTO chat_messages (conversation_id, role) VALUES (:c, 'user') RETURNING seq"),
                {"c": _SEQ_CONV_A},
            ).scalar()
        assert first == 1
    finally:
        _delete_seeded_chat(sync_engine)
        command.upgrade(alembic_cfg, "head")


def test_0010_fails_fast_when_chat_messages_is_held(alembic_cfg, sync_engine):
    """Beside a live API a chat turn holds chat_messages for its whole answer; 0010 waits at most 10 s for its lock
    (lock_timeout) rather than queueing every chat read behind it, says so, and leaves 0009 stamped."""
    import subprocess

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "0009_fts_over_folded_text")
    blocker = sync_engine.connect()
    blocker_tx = blocker.begin()
    try:
        blocker.execute(text("LOCK TABLE public.chat_messages IN ACCESS SHARE MODE"))
        started = time.monotonic()
        # A subprocess with a bound: without the lock_timeout the upgrade would wait for the blocker forever.
        run = subprocess.run(
            ["alembic", "upgrade", "head"], env=dict(os.environ), capture_output=True, text=True, timeout=60
        )
        elapsed = time.monotonic() - started
        assert run.returncode != 0
        assert "0010 could not lock public.chat_messages within 10s: another transaction holds it" in run.stderr, (
            run.stderr[-800:]
        )
        blocker_tx.rollback()
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0009_fts_over_folded_text"
        assert "seq" not in _chat_columns(sync_engine)
        assert 9 < elapsed < 30, elapsed
    finally:
        if blocker_tx.is_active:
            blocker_tx.rollback()
        blocker.close()
        command.upgrade(alembic_cfg, "head")
    assert "seq" in _chat_columns(sync_engine)


# ---------------------------------------------------------------------------
# 0011: the punctuation fold (#828)
# ---------------------------------------------------------------------------

_FOLD_DOC = "00000000-0000-0000-0000-000000000828"
# A zero-width space glues the words in every locale before 0011 (en_US.UTF-8 included), so the
# round trip below observes the recompute wherever the suite's database runs.
_FOLD_CHUNKS = [f"first{chr(0x200B)}refusal", f"the Party{chr(0x2019)}s obligations", "plain words"]


def _seed_fold_rows(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO documents (doc_id, title, sha256, pipeline_status, status) "
                "VALUES (:d, '0011 fold', '\\x0828'::bytea, 'ready', 'active')"
            ),
            {"d": _FOLD_DOC},
        )
        for n, chunk in enumerate(_FOLD_CHUNKS):
            conn.execute(
                text("INSERT INTO chunks (doc_id, chunk_num, chunk_text) VALUES (:d, :n, :t)"),
                {"d": _FOLD_DOC, "n": n, "t": chunk},
            )


def _delete_fold_rows(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE doc_id = :d"), {"d": _FOLD_DOC})


def _fold_rows(engine, fold: str) -> list[tuple]:
    """(chunk_num, fts_en::text, whether fts_en / fts_fr are what ``fold`` computes now) per seeded chunk."""
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    f"SELECT chunk_num, fts_en::text, "
                    f"fts_en = to_tsvector('english', public.{fold}(chunk_text)) "
                    f"AND fts_fr = to_tsvector('french', public.{fold}(chunk_text)) "
                    "FROM chunks WHERE doc_id = :d ORDER BY chunk_num"
                ),
                {"d": _FOLD_DOC},
            )
        ]


def _function_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT to_regprocedure(:f) IS NOT NULL"), {"f": f"public.{name}(text)"}).scalar()


def test_0011_round_trips_with_rows(alembic_cfg, sync_engine):
    """Down to 0010 and back up with rows present: each direction recomputes the stored columns of the
    rows already there, swaps the trigram index and the fold function, and keeps every row."""
    command.upgrade(alembic_cfg, "head")
    try:
        _seed_fold_rows(sync_engine)
        at_head = _fold_rows(sync_engine, "hc_fold_search_text")
        assert [r[2] for r in at_head] == [True] * 3
        assert "'refus'" in at_head[0][1] and "'parti'" in at_head[1][1], at_head

        command.downgrade(alembic_cfg, PRE_0011)

        assert _fts_expressions(sync_engine) == WS_FOLD_EXPRESSIONS
        at_0010 = _fold_rows(sync_engine, "hc_fold_whitespace")
        assert [r[2] for r in at_0010] == [True] * 3
        assert "'refus'" not in at_0010[0][1], at_0010
        assert _index_state(sync_engine, "ix_chunks_chunk_text_ws_trgm")[0] is True
        assert _index_state(sync_engine, HEAD_FOLD_INDEX) is None
        assert _function_exists(sync_engine, "hc_fold_whitespace")
        assert not _function_exists(sync_engine, "hc_fold_search_text")

        command.upgrade(alembic_cfg, "head")

        assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS
        assert _fold_rows(sync_engine, "hc_fold_search_text") == at_head
        valid, indexdef = _index_state(sync_engine, HEAD_FOLD_INDEX)
        assert valid is True and "hc_fold_search_text(chunk_text) gin_trgm_ops" in indexdef, indexdef
        assert _index_state(sync_engine, "ix_chunks_chunk_text_ws_trgm") is None
        assert not _function_exists(sync_engine, "hc_fold_whitespace")
        assert _function_exists(sync_engine, "hc_fold_search_text")
    finally:
        _delete_fold_rows(sync_engine)
        command.upgrade(alembic_cfg, "head")


def test_0011_refuses_a_fold_function_with_a_different_definition(alembic_cfg, sync_engine):
    """0008's guard, for the new name: an existing hc_fold_search_text with another body is never built on."""
    command.upgrade(alembic_cfg, "head")
    try:
        command.downgrade(alembic_cfg, PRE_0011)
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE FUNCTION public.hc_fold_search_text(text) RETURNS text "
                    "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE RETURN lower($1)"
                )
            )
        with pytest.raises(RuntimeError, match="different definition"):
            command.upgrade(alembic_cfg, "head")
        assert _fts_expressions(sync_engine) == WS_FOLD_EXPRESSIONS
    finally:
        with sync_engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if version == PRE_0011:
            with sync_engine.begin() as conn:
                conn.execute(text("DROP FUNCTION IF EXISTS public.hc_fold_search_text(text)"))
        command.upgrade(alembic_cfg, "head")
    assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS


def test_0011_fails_fast_when_chunks_is_held(alembic_cfg, sync_engine):
    """On a live API another transaction may hold chunks; 0011 waits at most 10 s for its lock
    (lock_timeout) rather than queueing every search behind it, says so, and leaves 0010 stamped."""
    import subprocess

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, PRE_0011)
    blocker = sync_engine.connect()
    blocker_tx = blocker.begin()
    try:
        blocker.execute(text("LOCK TABLE public.chunks IN ACCESS SHARE MODE"))
        started = time.monotonic()
        # A subprocess with a bound: without the lock_timeout the upgrade would wait for the blocker forever.
        run = subprocess.run(
            ["alembic", "upgrade", "head"], env=dict(os.environ), capture_output=True, text=True, timeout=60
        )
        elapsed = time.monotonic() - started
        assert run.returncode != 0
        assert "0011 could not lock public.chunks within 10s: another transaction holds it" in run.stderr, run.stderr[
            -800:
        ]
        blocker_tx.rollback()
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == PRE_0011
        assert _fts_expressions(sync_engine) == WS_FOLD_EXPRESSIONS
        assert not _function_exists(sync_engine, "hc_fold_search_text"), "the transaction did not roll back whole"
        assert 9 < elapsed < 30, elapsed
    finally:
        if blocker_tx.is_active:
            blocker_tx.rollback()
        blocker.close()
        command.upgrade(alembic_cfg, "head")
    assert _fts_expressions(sync_engine) == SEARCH_FOLD_EXPRESSIONS
