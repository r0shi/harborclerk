"""Tests for verify_schema_sentinel — panics out on binary↔DB mismatch."""

import pytest
from sqlalchemy import text

from harbor_clerk.db_health import SchemaSentinelMismatch, verify_schema_sentinel


async def _recreate_schema_metadata(db_session) -> None:
    """(Re)create the schema_metadata table and its three sentinel rows."""
    await db_session.execute(
        text("""
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key VARCHAR NOT NULL,
                value VARCHAR NOT NULL,
                set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (key)
            )
        """)
    )
    await db_session.execute(
        text("""
            INSERT INTO schema_metadata (key, value) VALUES
                ('embed_model', 'ibm-granite/granite-embedding-311m-multilingual-r2'),
                ('embed_dim', '768'),
                ('reranker', 'bge-reranker-v2-m3')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """)
    )
    await db_session.commit()


@pytest.fixture(autouse=True)
async def ensure_schema_metadata(db_session):
    """Ensure the schema_metadata table and sentinel rows exist before each test.

    The rebased 0001_initial migration creates this table, but the test DB may
    have been migrated with an older copy of 0001_initial (before the
    embedding-v2 rebase). This fixture idempotently creates the table and rows
    so the tests are self-contained regardless of DB history.

    Also recreates on teardown — test_verify_sentinel_raises_on_missing_table
    issues a committed DROP TABLE, which would poison subsequent test files.
    The rollback before teardown recreate is necessary because
    verify_schema_sentinel raises ProgrammingError when the table is missing,
    which leaves the session in an aborted-transaction state.
    """
    await _recreate_schema_metadata(db_session)
    yield
    # Rollback any aborted transaction before attempting DDL on teardown.
    await db_session.rollback()
    await _recreate_schema_metadata(db_session)


@pytest.mark.asyncio
async def test_verify_sentinel_passes_on_match(db_session, monkeypatch):
    """Sentinel rows match the settings — no exception."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "embed_model": "ibm-granite/granite-embedding-311m-multilingual-r2",
                "embed_dim": 768,
            },
        )(),
    )
    # db_session fixture loads the rebased migration which populates sentinel rows.
    # No exception should be raised.
    await verify_schema_sentinel(db_session)


@pytest.mark.asyncio
async def test_verify_sentinel_raises_on_model_mismatch(db_session, monkeypatch):
    """Sentinel says granite, settings say e5-small — must raise."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {"embed_model": "intfloat/multilingual-e5-small", "embed_dim": 384},
        )(),
    )
    with pytest.raises(SchemaSentinelMismatch) as exc_info:
        await verify_schema_sentinel(db_session)
    msg = str(exc_info.value)
    assert "embed_model" in msg
    assert "ibm-granite/granite-embedding-311m-multilingual-r2" in msg
    assert "multilingual-e5-small" in msg


@pytest.mark.asyncio
async def test_verify_sentinel_raises_on_missing_table(db_session, monkeypatch):
    """schema_metadata table doesn't exist — must raise distinctly."""
    await db_session.execute(text("DROP TABLE schema_metadata"))
    await db_session.commit()
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "embed_model": "ibm-granite/granite-embedding-311m-multilingual-r2",
                "embed_dim": 768,
            },
        )(),
    )
    with pytest.raises(SchemaSentinelMismatch) as exc_info:
        await verify_schema_sentinel(db_session)
    assert "schema_metadata" in str(exc_info.value)
