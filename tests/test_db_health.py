"""Tests for verify_schema_sentinel — panics out on binary↔DB mismatch."""

import pytest
from sqlalchemy import text

from harbor_clerk.db_health import SchemaSentinelMismatch, verify_schema_sentinel


@pytest.mark.asyncio
async def test_verify_sentinel_passes_on_match(db_session, monkeypatch):
    """Sentinel rows match the settings — no exception."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "embed_model": "granite-embedding-311m-multilingual-r2",
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
    assert "granite-embedding-311m-multilingual-r2" in msg
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
                "embed_model": "granite-embedding-311m-multilingual-r2",
                "embed_dim": 768,
            },
        )(),
    )
    with pytest.raises(SchemaSentinelMismatch) as exc_info:
        await verify_schema_sentinel(db_session)
    assert "schema_metadata" in str(exc_info.value)
