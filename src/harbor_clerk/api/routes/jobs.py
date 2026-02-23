"""SSE endpoint for job progress events."""

import asyncio
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.events import CHANNEL
from harbor_clerk.redis import get_async_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


async def _event_generator(redis) -> AsyncGenerator[str, None]:
    """Subscribe to Redis pub/sub and yield SSE events."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0,
            )
            if message is not None and message["type"] == "message":
                data = message["data"]
                yield f"data: {data}\n\n"
            else:
                # Send keepalive comment every ~15s of inactivity
                yield ": keepalive\n\n"
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()


@router.get("/jobs/stream")
async def job_stream(
    principal: Principal = Depends(require_read_access),
):
    redis = get_async_redis()
    return StreamingResponse(
        _event_generator(redis),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
