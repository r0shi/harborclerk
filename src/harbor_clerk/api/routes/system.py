import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.audit import log_audit
from harbor_clerk.db import get_session
from harbor_clerk.models import User
from harbor_clerk.storage import get_storage
from harbor_clerk.models import Chunk, Document, DocumentPage, DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/system/setup-status")
async def setup_status(
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Check whether initial setup is needed (no users exist)."""
    count = await session.scalar(select(func.count()).select_from(User))
    return {"needs_setup": count == 0}


@router.get("/system/health")
async def health_check(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Check connectivity to Postgres and storage."""
    checks: dict[str, Any] = {}

    # PostgreSQL
    try:
        result = await session.execute(text("SELECT 1"))
        result.scalar()
        checks["postgres"] = "ok"
    except Exception as e:
        logger.error("Postgres health check failed: %s", e)
        checks["postgres"] = f"error: {e}"

    # Storage
    try:
        storage = get_storage()
        storage.bucket_exists("originals")
        checks["storage"] = "ok"
    except Exception as e:
        logger.error("Storage health check failed: %s", e)
        checks["storage"] = f"error: {e}"

    overall = all(v == "ok" for v in checks.values())
    return {
        "status": "healthy" if overall else "degraded",
        "checks": checks,
    }


@router.get("/system/stats")
async def system_stats(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return per-service performance/health stats (admin only)."""
    result: dict[str, Any] = {}

    # ── PostgreSQL stats ──
    try:
        row = await session.execute(text(
            "SELECT pg_database_size(current_database()) AS db_size"
        ))
        db_size = row.scalar() or 0

        row = await session.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE state IS NOT NULL"
        ))
        active_conns = row.scalar() or 0

        row = await session.execute(text(
            "SELECT sum(heap_blks_hit)::float "
            "/ nullif(sum(heap_blks_hit + heap_blks_read), 0) "
            "FROM pg_statio_user_tables"
        ))
        cache_hit = row.scalar()

        row = await session.execute(text("SELECT count(*) FROM chunks"))
        total_chunks = row.scalar() or 0

        row = await session.execute(text(
            "SELECT coalesce(sum(n_dead_tup), 0) FROM pg_stat_user_tables"
        ))
        dead_tuples = row.scalar() or 0

        result["postgres"] = {
            "db_size_mb": round(db_size / (1024 * 1024), 1),
            "active_connections": int(active_conns),
            "cache_hit_ratio": round(float(cache_hit), 4) if cache_hit is not None else None,
            "total_chunks": int(total_chunks),
            "dead_tuples": int(dead_tuples),
        }
    except Exception as e:
        logger.error("Failed to collect Postgres stats: %s", e)
        result["postgres"] = {"error": str(e)}

    # ── Queue stats (from ingestion_jobs table) ──
    try:
        rows = await session.execute(text(
            "SELECT stage, count(*) FROM ingestion_jobs "
            "WHERE status = 'queued' GROUP BY stage"
        ))
        queue_depths = {row[0]: row[1] for row in rows}
        result["queues"] = {
            "io_queued": sum(
                queue_depths.get(s, 0)
                for s in ("extract", "chunk", "finalize")
            ),
            "cpu_queued": sum(
                queue_depths.get(s, 0)
                for s in ("ocr", "embed")
            ),
        }
    except Exception as e:
        logger.error("Failed to collect queue stats: %s", e)
        result["queues"] = {"error": str(e)}

    # ── Storage stats ──
    try:
        storage = get_storage()
        objects = storage.list_objects("originals", recursive=True)
        obj_count = len(objects)
        total_size = sum(o["size"] for o in objects)
        result["storage"] = {
            "object_count": obj_count,
            "total_size_mb": round(total_size / (1024 * 1024), 1),
        }
    except Exception as e:
        logger.error("Failed to collect storage stats: %s", e)
        result["storage"] = {"error": str(e)}

    return result


@router.post("/system/purge-run")
async def purge_run(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Hard-delete documents soft-deleted more than 60 days ago, including MinIO objects."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    result = await session.execute(
        select(Document).where(
            Document.status == "deleted",
            Document.updated_at < cutoff,
        )
    )
    docs = result.scalars().all()

    if not docs:
        return {"purged": 0}

    storage = get_storage()
    purged = 0

    for doc in docs:
        # Load versions for storage cleanup
        versions_result = await session.execute(
            select(DocumentVersion).where(DocumentVersion.doc_id == doc.doc_id)
        )
        versions = versions_result.scalars().all()

        for ver in versions:
            # Delete stored object
            try:
                storage.remove_object(ver.original_bucket, ver.original_object_key)
            except Exception as e:
                logger.warning(
                    "Failed to delete object %s/%s: %s",
                    ver.original_bucket, ver.original_object_key, e,
                )

            # Cascade delete DB rows: chunks, pages, ingestion_jobs
            await session.execute(
                delete(Chunk).where(Chunk.version_id == ver.version_id)
            )
            await session.execute(
                delete(DocumentPage).where(DocumentPage.version_id == ver.version_id)
            )
            await session.execute(
                delete(IngestionJob).where(IngestionJob.version_id == ver.version_id)
            )

        # Delete versions and document
        await session.execute(
            delete(DocumentVersion).where(DocumentVersion.doc_id == doc.doc_id)
        )
        await session.delete(doc)
        purged += 1

    await log_audit(
        session, user_id=admin.id, action="purge_run",
        detail={"purged_count": purged},
    )
    await session.commit()

    logger.info("Purged %d documents", purged)
    return {"purged": purged}


@router.post("/system/reaper-run")
async def reaper_run(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Find ingestion jobs stuck as running past 2x their timeout, re-enqueue them."""
    from harbor_clerk.worker.pipeline import STAGE_CONFIG, enqueue_stage

    # Get all currently running jobs from DB
    result = await session.execute(
        select(IngestionJob).where(IngestionJob.status == JobStatus.running)
    )
    running_jobs = result.scalars().all()

    if not running_jobs:
        return {"reaped": 0}

    reaped = 0
    for job in running_jobs:
        _, timeout, _ = STAGE_CONFIG[job.stage]
        if job.started_at is None:
            continue
        elapsed = (datetime.now(timezone.utc) - job.started_at).total_seconds()
        if elapsed < timeout * 2:
            continue

        # Orphan detected — re-enqueue
        logger.warning(
            "Reaping orphan job: version=%s stage=%s (running for %.0fs, timeout=%ds)",
            job.version_id, job.stage.value, elapsed, timeout,
        )
        await session.commit()  # flush before sync enqueue_stage
        enqueue_stage(job.version_id, job.stage)
        reaped += 1

    await log_audit(
        session, user_id=admin.id, action="reaper_run",
        detail={"reaped_count": reaped},
    )
    await session.commit()

    logger.info("Reaped %d orphan jobs", reaped)
    return {"reaped": reaped}
