"""SSE endpoint for job progress events via PostgreSQL LISTEN/NOTIFY."""

import asyncio
import logging
from collections.abc import AsyncGenerator

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.events import CHANNEL
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.ingestion_job import IngestionJob

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


@router.get("/jobs/active")
async def active_jobs(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return all non-terminal ingestion jobs for queue tray backfill on page load.

    Returns the same shape as SSE JobEvent so the frontend can feed these
    directly into the same onEvent handler.
    """
    rows = (
        await session.execute(
            select(
                IngestionJob.version_id,
                IngestionJob.stage,
                IngestionJob.status,
                IngestionJob.progress_current,
                IngestionJob.progress_total,
                IngestionJob.error,
                DocumentVersion.doc_id,
                Document.canonical_filename,
            )
            .join(DocumentVersion, IngestionJob.version_id == DocumentVersion.version_id)
            .join(Document, DocumentVersion.doc_id == Document.doc_id)
            .where(IngestionJob.status.in_(["queued", "running"]))
            .order_by(IngestionJob.created_at)
        )
    ).all()

    events = []
    for r in rows:
        event: dict = {
            "version_id": str(r.version_id),
            "stage": r.stage.value if hasattr(r.stage, "value") else str(r.stage),
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
        }
        if r.progress_current:
            event["progress"] = r.progress_current
        if r.progress_total:
            event["total"] = r.progress_total
        if r.error:
            event["error"] = r.error
        if r.canonical_filename:
            event["filename"] = r.canonical_filename
        if r.doc_id:
            event["doc_id"] = str(r.doc_id)
        events.append(event)

    return events
