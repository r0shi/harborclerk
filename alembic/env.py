import asyncio
import logging
import os
import time
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from harbor_clerk.migration_session import (
    LOCK_HOLDER_PARAMS,
    LOCK_HOLDER_SQL,
    MIGRATION_LOCK_KEY,
    MIGRATION_LOCK_POLL_SECONDS,
    application_name,
)

# Import all models so they register on Base.metadata
from harbor_clerk.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which switches off every logger
    # not named in alembic.ini — including all of harbor_clerk.*. Production is
    # unaffected because migrations run as a subprocess (the /system/run-migrations
    # route and MigrationRunner.swift both shell out), but tests/test_migrations.py
    # calls alembic in-process, so it silently disabled harbor_clerk logging for
    # every test that ran after it and made any later log assertion vacuous.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _acquire_migration_lock(connection: Connection) -> None:
    """Wait for the migration lock (harbor_clerk.migration_session.MIGRATION_LOCK_KEY) by polling.

    Never pg_advisory_lock: a backend blocked in it holds a snapshot, and CREATE INDEX CONCURRENTLY
    (0008) waits for every older snapshot in the database, while this waiter waits on the lock
    that build's session holds: a deadlock, and the detector killed one of the two runs (the one
    building, mid-build, left an INVALID index). Each try is its own transaction, ended at once,
    so nothing is held between tries; a transaction left open across tries would turn the detected
    deadlock into an undetected hang (the build waits for that transaction to end).
    """
    log = logging.getLogger("alembic.env")
    waiting = False
    while True:
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}).scalar()
        connection.commit()
        if acquired:
            if waiting:
                log.info("Another migration run finished; continuing")
            return
        if not waiting:
            holder = connection.execute(text(LOCK_HOLDER_SQL), LOCK_HOLDER_PARAMS).first()
            connection.commit()
            pid, name = holder if holder else (None, "")
            log.info("Waiting for another migration run to finish (held by pid %s, application_name %r)", pid, name)
            waiting = True
        time.sleep(MIGRATION_LOCK_POLL_SECONDS)


def do_run_migrations(connection: Connection) -> None:
    # One run at a time: a session-level advisory lock, so it spans the commits of an autocommit
    # block (0008) and is released when this connection closes even if the unlock below never
    # runs. Acquired by polling (see _acquire_migration_lock) and committed: alembic would
    # otherwise find a transaction already open and never commit its own.
    _acquire_migration_lock(connection)
    try:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        try:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()
        except Exception:  # never mask the migration's own error; closing the connection unlocks
            logging.getLogger("alembic.env").warning("advisory unlock failed; released on disconnect")


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Named so a caller that gives up on this run can cancel its backend
        # (harbor_clerk.migration_session): killing alembic does not stop it.
        connect_args={"server_settings": {"application_name": application_name()}},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
