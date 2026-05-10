"""SSE endpoint for job progress events via PostgreSQL LISTEN/NOTIFY."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.events import CHANNEL
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import JobStage
from harbor_clerk.models.ingestion_job import IngestionJob, JobStatus
from harbor_clerk.worker.pipeline import STAGE_CONFIG, STAGE_ORDER

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
                IngestionJob.doc_id,
                IngestionJob.stage,
                IngestionJob.status,
                IngestionJob.progress_current,
                IngestionJob.progress_total,
                IngestionJob.error,
                Document.canonical_filename,
            )
            .join(Document, IngestionJob.doc_id == Document.doc_id)
            .where(IngestionJob.status.in_(["queued", "running"]))
            .order_by(IngestionJob.created_at)
        )
    ).all()

    events = []
    for r in rows:
        event: dict = {
            "doc_id": str(r.doc_id),
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
        events.append(event)

    return events


@router.get("/jobs/snapshot")
async def jobs_snapshot(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Per-stage queued/running counts and recent throughput.

    Returns:
        {
            "by_stage": {
                "extract": {"queued": 0, "running": 1, "queue": "io",
                            "recent_completed": 5},
                "ocr":     {"queued": 3, "running": 0, "queue": "cpu",
                            "recent_completed": 0},
                ...
            },
            "queues": {
                "io":  {"queued": 0, "running": 1},
                "cpu": {"queued": 3, "running": 0},
            },
            "stage_order": ["extract", "ocr", "chunk", ...],  // pipeline order
            "throughput_window_seconds": 30,
        }

    `recent_completed` is the count of jobs that finished at each stage
    in the last `throughput_window_seconds`. Drives the Observatory
    pipeline-flow visualisation (edge thickness, particle spawn rate).

    Polled by the frontend every few seconds. Cheap: two indexed
    aggregations on `ingestion_jobs`, no joins.
    """
    # Counts of currently-queued / currently-running jobs by stage.
    rows = (
        await session.execute(
            select(IngestionJob.stage, IngestionJob.status, func.count())
            .where(IngestionJob.status.in_([JobStatus.queued, JobStatus.running]))
            .group_by(IngestionJob.stage, IngestionJob.status)
        )
    ).all()

    # Recently-completed jobs by stage for the throughput indicator.
    # 30 s window matches the rolling visualisation cadence — long
    # enough to smooth bursts, short enough that the diagram tracks
    # current activity rather than historical state.
    window_seconds = 30
    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    completion_rows = (
        await session.execute(
            select(IngestionJob.stage, func.count())
            .where(IngestionJob.status == JobStatus.done, IngestionJob.finished_at >= cutoff)
            .group_by(IngestionJob.stage)
        )
    ).all()

    by_stage: dict[str, dict[str, int | str]] = {}
    queues: dict[str, dict[str, int]] = {
        "io": {"queued": 0, "running": 0},
        "cpu": {"queued": 0, "running": 0},
        "llm": {"queued": 0, "running": 0},
    }

    # Seed every stage with zeros so the frontend doesn't need to
    # special-case "stage absent from response = no work".
    for stage in STAGE_ORDER:
        queue_name, _, _ = STAGE_CONFIG[stage]
        by_stage[stage.value] = {"queued": 0, "running": 0, "queue": queue_name, "recent_completed": 0}

    for stage, status, count in rows:
        stage_key = stage.value if hasattr(stage, "value") else str(stage)
        status_key = status.value if hasattr(status, "value") else str(status)
        if stage_key not in by_stage or status_key not in ("queued", "running"):
            continue
        by_stage[stage_key][status_key] = int(count)
        # Mirror into the IO/CPU rollup.
        queue_name = by_stage[stage_key]["queue"]
        if queue_name in queues:
            queues[queue_name][status_key] += int(count)

    for stage, count in completion_rows:
        stage_key = stage.value if hasattr(stage, "value") else str(stage)
        if stage_key in by_stage:
            by_stage[stage_key]["recent_completed"] = int(count)

    # Fetch in-flight summarize jobs (queued + running) themselves — not just
    # counts — so the queue tray can render per-doc rows with map-reduce progress.
    summarizing_rows = (
        await session.execute(
            select(IngestionJob, Document)
            .join(Document, IngestionJob.doc_id == Document.doc_id)
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status.in_((JobStatus.queued, JobStatus.running)))
            .order_by(IngestionJob.created_at)
        )
    ).all()

    summarizing = [
        {
            "doc_id": str(job.doc_id),
            "filename": doc.canonical_filename or doc.title,
            "status": job.status.value,
            "progress_current": job.progress_current or 0,
            "progress_total": job.progress_total or 0,
            "created_at": job.created_at.isoformat(),
        }
        for job, doc in summarizing_rows
    ]

    return {
        "by_stage": by_stage,
        "queues": queues,
        "stage_order": [s.value for s in STAGE_ORDER],
        "throughput_window_seconds": window_seconds,
        "summarizing": summarizing,
    }
