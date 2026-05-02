"""Test the 0018 enum rename: version_status -> pipeline_status."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

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


def _enum_typename_exists(conn, typename: str) -> bool:
    """True if a Postgres enum type with this name exists."""
    return bool(
        conn.execute(
            text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": typename},
        ).scalar()
    )


def test_0018_renames_version_status_to_pipeline_status(alembic_cfg, sync_engine):
    """At revision 0017 the enum is named version_status; after 0018 it is
    pipeline_status. Round-tripping back to 0017 restores the original name."""
    # Establish a clean baseline at 0017
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0017")

    with sync_engine.connect() as conn:
        assert _enum_typename_exists(conn, "version_status"), "Expected version_status enum to exist at revision 0017"
        assert not _enum_typename_exists(conn, "pipeline_status"), (
            "Expected pipeline_status enum to NOT exist at revision 0017"
        )

    # Apply the rename
    command.upgrade(alembic_cfg, "0018")

    with sync_engine.connect() as conn:
        assert _enum_typename_exists(conn, "pipeline_status"), (
            "Expected pipeline_status enum to exist after 0018 upgrade"
        )
        assert not _enum_typename_exists(conn, "version_status"), (
            "Expected version_status enum to be gone after 0018 upgrade"
        )

    # Roll back the rename
    command.downgrade(alembic_cfg, "0017")

    with sync_engine.connect() as conn:
        assert _enum_typename_exists(conn, "version_status"), (
            "Expected version_status enum to be restored after 0018 downgrade"
        )
        assert not _enum_typename_exists(conn, "pipeline_status"), (
            "Expected pipeline_status enum to be gone after 0018 downgrade"
        )


def test_0018_preserves_documents_pipeline_status_column_values(alembic_cfg, sync_engine):
    """Renaming the type must not affect existing rows that use it. Insert a
    document at 0017 with pipeline_status='ready', upgrade to 0018, verify
    the row still reads back correctly."""
    import hashlib
    import uuid

    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0017")

    doc_id = uuid.uuid4()
    sha = hashlib.sha256(b"enum-rename-fixture").digest()

    with sync_engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO documents (doc_id, title, status, sha256, pipeline_status, created_at, updated_at)
                VALUES (:did, 'Enum rename fixture', 'active', :sha, 'ready', now(), now())
            """),
            {"did": doc_id, "sha": sha},
        )

    command.upgrade(alembic_cfg, "0018")

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT pipeline_status FROM documents WHERE doc_id = :did"),
            {"did": doc_id},
        ).first()
        assert row is not None
        # Cast to text for the assertion since the column type changed names
        assert str(row.pipeline_status) == "ready"

        # Verify the column type is the renamed one
        type_name = conn.execute(
            text("""
                SELECT t.typname
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_type t ON t.oid = a.atttypid
                WHERE c.relname = 'documents' AND a.attname = 'pipeline_status'
            """)
        ).scalar()
        assert type_name == "pipeline_status"
