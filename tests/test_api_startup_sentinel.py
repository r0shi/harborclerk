"""Verify the API lifespan calls panic_on_sentinel_mismatch."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_calls_sentinel_check():
    """Boot path must include panic_on_sentinel_mismatch before serving."""
    from harbor_clerk.api.app import app

    with patch("harbor_clerk.api.app.panic_on_sentinel_mismatch", new=AsyncMock()) as mock_panic:
        async with app.router.lifespan_context(app):
            pass
        mock_panic.assert_awaited()
