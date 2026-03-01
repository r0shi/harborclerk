"""SSE endpoint for job progress events via PostgreSQL LISTEN/NOTIFY."""

import asyncio
import logging
from collections.abc import AsyncGenerator

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.config import get_settings
from harbor_clerk.events import CHANNEL

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])

KEEPALIVE_INTERVAL = 15  # seconds


def _asyncpg_dsn() -> str:
    """Convert SQLAlchemy DSN to raw asyncpg DSN."""
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def _event_generator() -> AsyncGenerator[str, None]:
    """Listen on PostgreSQL NOTIFY channel and yield SSE events."""
    queue: asyncio.Queue[str] = asyncio.Queue()

    def _on_notify(conn, pid, channel, payload):
        queue.put_nowait(payload)

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.add_listener(CHANNEL, _on_notify)
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                yield f"data: {payload}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        await conn.remove_listener(CHANNEL, _on_notify)
        await conn.close()


@router.get("/jobs/stream")
async def job_stream(
    principal: Principal = Depends(require_read_access),
):
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
