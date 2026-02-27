"""PostgreSQL NOTIFY event publisher for job progress."""

import json
import logging
import uuid

from sqlalchemy import text

from harbor_clerk.db_sync import get_sync_session

logger = logging.getLogger(__name__)

CHANNEL = "job_progress"


def publish_job_event(
    version_id: uuid.UUID,
    stage: str,
    status: str,
    progress: int | None = None,
    total: int | None = None,
    error: str | None = None,
    filename: str | None = None,
    doc_id: uuid.UUID | None = None,
    page_count: int | None = None,
    chunk_count: int | None = None,
) -> None:
    """Publish a job progress event via PostgreSQL NOTIFY (sync, for workers)."""
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
    if filename is not None:
        payload["filename"] = filename
    if doc_id is not None:
        payload["doc_id"] = str(doc_id)
    if page_count is not None:
        payload["page_count"] = page_count
    if chunk_count is not None:
        payload["chunk_count"] = chunk_count

    try:
        session = get_sync_session()
        try:
            session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": CHANNEL, "payload": json.dumps(payload)},
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("Failed to publish job event")
