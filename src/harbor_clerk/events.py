"""Redis pub/sub event publisher for job progress."""

import json
import logging
import uuid

from redis import Redis

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)

CHANNEL = "job_progress"


def _get_sync_redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)


def publish_job_event(
    version_id: uuid.UUID,
    stage: str,
    status: str,
    progress: int | None = None,
    total: int | None = None,
    error: str | None = None,
) -> None:
    """Publish a job progress event via Redis pub/sub (sync, for workers)."""
    payload = {
        "version_id": str(version_id),
        "stage": stage,
        "status": status,
    }
    if progress is not None:
        payload["progress"] = progress
    if total is not None:
        payload["total"] = total
    if error is not None:
        payload["error"] = error

    try:
        r = _get_sync_redis()
        r.publish(CHANNEL, json.dumps(payload))
        r.close()
    except Exception:
        logger.exception("Failed to publish job event")
