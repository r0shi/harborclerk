"""Verify the API lifespan calls panic_on_sentinel_mismatch and checks WATCH_ROOT.

One test: the lifespan runs the MCP session manager, which can only be started
once per process.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_calls_sentinel_check(monkeypatch, tmp_path):
    """Boot path must include panic_on_sentinel_mismatch before serving.

    It also checks WATCH_ROOT: one that cannot work is an ERROR at startup, not
    a silent empty folder list (#871).
    """
    import harbor_clerk.config as config
    from harbor_clerk.api.app import app

    monkeypatch.setenv("WATCH_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(config, "_settings", None)

    with (
        patch("harbor_clerk.api.app.panic_on_sentinel_mismatch", new=AsyncMock()) as mock_panic,
        patch("harbor_clerk.watcher.discovery.log_watch_root_problem") as mock_watch_root_check,
    ):
        async with app.router.lifespan_context(app):
            pass
        mock_panic.assert_awaited()
    mock_watch_root_check.assert_called_once_with(str(tmp_path / "missing"), "api")
