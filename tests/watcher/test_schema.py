"""Schema-level tests for the watched_folders table after migration 0016."""

import pytest
from sqlalchemy import inspect


@pytest.mark.asyncio
async def test_watched_folders_has_new_columns(_engine):
    async with _engine.connect() as conn:
        cols = await conn.run_sync(
            lambda sync_conn: {c["name"]: c for c in inspect(sync_conn).get_columns("watched_folders")}
        )
    assert "unavailable_reason" in cols
    assert "display_name" in cols
    assert "auto_discovered" in cols
    assert cols["auto_discovered"]["nullable"] is False
    assert cols["bookmark_data"]["nullable"] is True
