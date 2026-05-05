"""IDLE supervisor + polling fallback for the per-label sync loop.

`poll_or_idle_loop(conn, on_tick, poll_interval)` runs forever, calling
`on_tick(conn)` whenever the server signals new mail (via IDLE EXISTS) or
the poll interval expires. The caller raises CancelledError to stop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from harbor_clerk.mail.imap_client import IMAPConnection

logger = logging.getLogger(__name__)


async def server_supports_idle(conn: IMAPConnection) -> bool:
    """Issue CAPABILITY and check for IDLE."""
    result, lines = await conn.client.capability()
    if result != "OK":
        return False
    blob = b" ".join(lines).upper()
    return b"IDLE" in blob.split()


async def poll_or_idle_loop(
    conn: IMAPConnection,
    on_tick: Callable[[IMAPConnection], Awaitable[None]],
    poll_interval: float = 120.0,
    idle_timeout: float = 29 * 60,  # Gmail recommends < 30 minutes
) -> None:
    """Run forever, calling on_tick on each IDLE EXISTS or poll timeout.

    Strategy:
      - Probe CAPABILITY for IDLE.
      - If supported: open IDLE, race wait_server_push against asyncio
        timeout=idle_timeout. On either, exit IDLE, call on_tick, restart.
      - If not supported: sleep poll_interval, call on_tick, loop.

    The callback exiting via CancelledError ends the loop.
    """
    if await server_supports_idle(conn):
        logger.info("conn %s: using IDLE (timeout=%.0fs)", conn.host, idle_timeout)
        while True:
            try:
                await conn.client.idle_start(timeout=idle_timeout)
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(conn.client.wait_server_push(), timeout=idle_timeout)
                        except TimeoutError:
                            break  # IDLE refresh; no events
                        if b"EXISTS" in event or b"EXPUNGE" in event:
                            break
                finally:
                    await conn.client.idle_done()
                await on_tick(conn)
            except asyncio.CancelledError:
                # Inner `finally` already called idle_done(); calling it twice would
                # send DONE\r\n with no IDLE session open — protocol error against
                # real IMAP servers (Gmail, Exchange).
                raise
    else:
        logger.info("conn %s: IDLE unsupported, polling every %.0fs", conn.host, poll_interval)
        while True:
            try:
                await on_tick(conn)
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
