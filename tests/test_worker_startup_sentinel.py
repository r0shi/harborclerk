"""Verify the worker entry calls panic_on_sentinel_mismatch before pulling jobs.

Adaptation note: entry.py's main() is synchronous (no async_main/_run_worker_loop).
The sentinel check is exposed as _check_sentinel() — a sync wrapper around the
async panic_on_sentinel_mismatch. We patch _check_sentinel so the test runs without
an event loop conflict, and verify it is called before the polling loop starts.
"""

import sys
from unittest.mock import MagicMock, patch


def test_worker_main_calls_sentinel_check():
    """The worker boot path must invoke _check_sentinel (which wraps
    panic_on_sentinel_mismatch) before starting the polling loop."""
    from harbor_clerk.worker import entry

    # Build a minimal mock listen connection that satisfies the polling loop.
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    call_order: list[str] = []

    def sentinel_side_effect():
        call_order.append("sentinel")

    def shutdown_side_effect(*args, **kwargs):
        call_order.append("loop")
        # Signal shutdown so the while loop exits immediately
        entry._shutdown = True

    with (
        patch.object(entry, "_check_sentinel", side_effect=sentinel_side_effect) as mock_check,
        patch.object(entry, "_get_listen_connection", return_value=mock_conn),
        patch.object(entry, "claim_next_job", side_effect=shutdown_side_effect),
        patch.object(entry, "_wait_for_notify"),
        patch.object(sys, "argv", ["harbor-clerk-worker", "--queues", "io"]),
    ):
        original_shutdown = entry._shutdown
        entry._shutdown = False
        try:
            entry.main()
        finally:
            entry._shutdown = original_shutdown

    mock_check.assert_called_once()
    # Sentinel must have been called before any polling work
    assert call_order.index("sentinel") < call_order.index("loop")
