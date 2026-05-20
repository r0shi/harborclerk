import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.system import (
    DeleteAllRequest,
    RetrievalSettingsResponse,
    RetrievalSettingsUpdate,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.config import get_settings, sync_native_config
from harbor_clerk.db import get_session
from harbor_clerk.models import (
    Chunk,
    Document,
    DocumentPage,
    IngestionJob,
    User,
)
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.storage import get_storage

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

    # Tika
    try:
        settings = get_settings()
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.tika_url}/tika", timeout=5)
            checks["tika"] = "ok" if r.status_code == 200 else f"error: HTTP {r.status_code}"
    except Exception as e:
        logger.error("Tika health check failed: %s", e)
        checks["tika"] = f"error: {e}"

    # Reranker — only probed when enabled; disabled is not a degraded state
    settings = get_settings()
    if settings.reranker_enabled:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{settings.reranker_url}/health", timeout=5)
                checks["reranker"] = "ok" if r.status_code == 200 else f"error: HTTP {r.status_code}"
        except Exception as e:
            logger.error("Reranker health check failed: %s", e)
            checks["reranker"] = f"error: {e}"
    else:
        checks["reranker"] = "disabled"

    from harbor_clerk.api.app import BUILD_HASH

    overall = all(v in ("ok", "disabled") for v in checks.values())
    return {
        "status": "healthy" if overall else "degraded",
        "build": BUILD_HASH,
        "checks": checks,
        # Deployment-wide capabilities the frontend needs to know about. Kept
        # in the health response so it's fetched on app boot without a
        # separate round-trip — see frontend/src/hooks/useSystemConfig.ts.
        "allow_source_download": get_settings().allow_source_download,
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
        row = await session.execute(text("SELECT pg_database_size(current_database()) AS db_size"))
        db_size = row.scalar() or 0

        row = await session.execute(text("SELECT count(*) FROM pg_stat_activity WHERE state IS NOT NULL"))
        active_conns = row.scalar() or 0

        row = await session.execute(
            text(
                "SELECT sum(heap_blks_hit)::float "
                "/ nullif(sum(heap_blks_hit + heap_blks_read), 0) "
                "FROM pg_statio_user_tables"
            )
        )
        cache_hit = row.scalar()

        row = await session.execute(text("SELECT count(*) FROM chunks"))
        total_chunks = row.scalar() or 0

        row = await session.execute(text("SELECT coalesce(sum(n_dead_tup), 0) FROM pg_stat_user_tables"))
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
        rows = await session.execute(
            text("SELECT stage, count(*) FROM ingestion_jobs WHERE status = 'queued' GROUP BY stage")
        )
        queue_depths = {row[0]: row[1] for row in rows}
        result["queues"] = {
            "io_queued": sum(queue_depths.get(s, 0) for s in ("extract", "chunk", "finalize")),
            "cpu_queued": sum(queue_depths.get(s, 0) for s in ("ocr", "embed")),
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
    cutoff = datetime.now(UTC) - timedelta(days=60)

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
        # Delete stored object if present
        if doc.original_bucket and doc.original_object_key:
            try:
                storage.remove_object(doc.original_bucket, doc.original_object_key)
            except Exception as e:
                logger.warning(
                    "Failed to delete object %s/%s: %s",
                    doc.original_bucket,
                    doc.original_object_key,
                    e,
                )

        # Cascade delete child rows (FK cascades handle most, but explicit for clarity)
        await session.execute(delete(Chunk).where(Chunk.doc_id == doc.doc_id))
        await session.execute(delete(DocumentPage).where(DocumentPage.doc_id == doc.doc_id))
        await session.execute(delete(IngestionJob).where(IngestionJob.doc_id == doc.doc_id))
        await session.delete(doc)
        purged += 1

    await log_audit(
        session,
        user_id=admin.id,
        action="purge_run",
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
    """Find ingestion jobs stuck as running with stale heartbeat, re-enqueue them."""
    from harbor_clerk.worker.pipeline import STAGE_CONFIG, enqueue_stage

    # Get all currently running jobs from DB
    result = await session.execute(select(IngestionJob).where(IngestionJob.status == JobStatus.running))
    running_jobs = result.scalars().all()

    if not running_jobs:
        return {"reaped": 0}

    now = datetime.now(UTC)
    orphans: list[tuple[uuid.UUID, JobStage]] = []
    for job in running_jobs:
        if job.heartbeat_at is not None:
            # Worker has heartbeat — if stale > 90s, it's dead
            stale = (now - job.heartbeat_at).total_seconds()
            if stale < 90:
                continue
        else:
            # Legacy: no heartbeat yet, use 2x timeout
            _, timeout, _ = STAGE_CONFIG[job.stage]
            if job.started_at is None:
                continue
            elapsed = (now - job.started_at).total_seconds()
            if elapsed < timeout * 2:
                continue

        logger.warning(
            "Reaping orphan job: doc=%s stage=%s heartbeat_at=%s",
            job.doc_id,
            job.stage.value,
            job.heartbeat_at,
        )
        orphans.append((job.doc_id, job.stage))

    # Commit any pending state and log audit before re-enqueuing
    await log_audit(
        session,
        user_id=admin.id,
        action="reaper_run",
        detail={"reaped_count": len(orphans)},
    )
    await session.commit()

    # Re-enqueue orphans (each creates its own sync session)
    for doc_id, stage in orphans:
        enqueue_stage(doc_id, stage)

    logger.info("Reaped %d orphan jobs", len(orphans))
    return {"reaped": len(orphans)}


@router.post("/system/recompute-topics")
async def recompute_topics_endpoint(
    admin: Principal = Depends(require_admin),
):
    """Trigger topic recomputation as a fire-and-forget background task.

    BERTopic + numba JIT is CPU-intensive (minutes on first run).
    Returns immediately; check /stats/topics for results.
    """
    import asyncio

    from harbor_clerk.topics import recompute_topics

    asyncio.create_task(recompute_topics())
    return {"status": "started", "message": "Topic computation running in background. Check /stats/topics for results."}


@router.post("/system/clear-queue")
async def clear_queue(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cancel all queued and running ingestion jobs, reset documents to their last stable state.

    Use when the queue is in a bad state from bugs/restarts and needs a clean slate.
    Does NOT delete documents or re-trigger ingestion — just clears stuck jobs.
    """
    # Cancel all queued/running jobs
    result = await session.execute(
        select(IngestionJob).where(IngestionJob.status.in_([JobStatus.queued, JobStatus.running]))
    )
    jobs = result.scalars().all()
    cancelled = 0
    doc_ids_affected: set[uuid.UUID] = set()
    for job in jobs:
        job.status = JobStatus.error
        job.error = "Cancelled by admin (queue clear)"
        doc_ids_affected.add(job.doc_id)
        cancelled += 1

    # Reset affected documents: if finalize completed, set ready; otherwise error
    for did in doc_ids_affected:
        doc = await session.get(Document, did)
        if doc and doc.pipeline_status not in (PipelineStatus.ready, PipelineStatus.error):
            fin_result = await session.execute(
                select(IngestionJob).where(
                    IngestionJob.doc_id == did,
                    IngestionJob.stage == JobStage.finalize,
                    IngestionJob.status == JobStatus.done,
                )
            )
            if fin_result.scalar_one_or_none():
                doc.pipeline_status = PipelineStatus.ready
            else:
                doc.pipeline_status = PipelineStatus.error
                doc.error = "Queue cleared before pipeline completed"

    await log_audit(session, user_id=admin.id, action="clear_queue", detail={"cancelled": cancelled})
    await session.commit()

    logger.info("Queue cleared: %d jobs cancelled, %d documents affected", cancelled, len(doc_ids_affected))
    return {"cancelled": cancelled, "versions_affected": len(doc_ids_affected)}


@router.post("/system/run-migrations")
async def run_migrations(
    admin: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Run alembic upgrade head. Returns current schema version after completion."""
    import asyncio
    import subprocess

    settings = get_settings()

    def _run():
        result = subprocess.run(
            ["python", "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "DATABASE_URL": settings.database_url,
            },
        )
        return result

    try:
        result = await asyncio.to_thread(_run)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Migration failed: {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Migration timed out after 120s")

    # Check resulting version
    from harbor_clerk.db import async_session_factory

    async with async_session_factory() as db:
        ver_result = await db.execute(text("SELECT version_num FROM alembic_version"))
        row = ver_result.first()
        version = row[0] if row else "unknown"

    return {"status": "ok", "schema_version": version}


@router.post("/system/reprocess-all")
async def reprocess_all(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-run ingestion pipeline on every active document's latest version."""
    from harbor_clerk.worker.pipeline import enqueue_stage, reset_jobs

    result = await session.execute(select(Document).where(Document.status == "active"))
    docs = result.scalars().all()

    count = 0
    for doc in docs:
        doc.pipeline_status = PipelineStatus.queued
        doc.pipeline_seq = (doc.pipeline_seq or 0) + 1
        doc.error = None
        count += 1

    await log_audit(
        session,
        user_id=admin.id,
        action="reprocess_all",
        detail={"reprocessed_count": count},
    )
    await session.commit()

    # Reset and re-enqueue outside the async session (sync calls)
    for doc in docs:
        reset_jobs(doc.doc_id)
        enqueue_stage(doc.doc_id, JobStage.extract)

    logger.info("Reprocess-all: %d documents queued", count)
    return {"reprocessed": count}


@router.post("/system/resummarize-all")
async def resummarize_all(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Re-run only the summarize stage on every active document's latest version.

    Useful after upgrading the LLM model or changing summary settings.
    Skips documents that have no chunks (not yet fully ingested).
    """
    from harbor_clerk.worker.pipeline import enqueue_stage

    # Find active documents that are ready (pipeline complete)
    result = await session.execute(
        select(Document.doc_id).where(
            Document.status == "active",
            Document.pipeline_status == PipelineStatus.ready,
        )
    )
    doc_ids_ready = [row[0] for row in result.all()]

    await log_audit(
        session,
        user_id=admin.id,
        action="resummarize_all",
        detail={"resummarized_count": len(doc_ids_ready)},
    )
    await session.commit()

    # Enqueue summarize jobs outside the async session (sync calls)
    count = 0
    for did in doc_ids_ready:
        enqueue_stage(did, JobStage.summarize)
        count += 1

    logger.info("Resummarize-all: %d documents queued", count)
    return {"resummarized": count}


@router.post("/system/delete-all-documents")
async def delete_all_documents(
    body: DeleteAllRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Delete ALL documents and related data. Keeps users, api_keys, audit_log, conversations, chat_messages."""
    if body.confirmation != "DELETE EVERYTHING":
        raise HTTPException(
            status_code=400,
            detail="Confirmation text must be exactly 'DELETE EVERYTHING'",
        )

    # Delete all storage objects
    try:
        storage = get_storage()
        objects = storage.list_objects("originals", recursive=True)
        for obj in objects:
            try:
                storage.remove_object("originals", obj["key"])
            except Exception as e:
                logger.warning("Failed to delete storage object %s: %s", obj["key"], e)
    except Exception as e:
        logger.warning("Failed to list storage objects for deletion: %s", e)

    # TRUNCATE CASCADE all document-related tables
    await session.execute(
        text("TRUNCATE entities, chunks, document_headings, document_pages, ingestion_jobs, documents, uploads CASCADE")
    )

    await log_audit(
        session,
        user_id=admin.id,
        action="delete_all_documents",
        detail={},
    )
    await session.commit()

    logger.info("All documents deleted by admin %s", admin.id)
    return {"status": "ok"}


@router.get("/system/retrieval-settings")
async def get_retrieval_settings(
    admin: Principal = Depends(require_admin),
) -> RetrievalSettingsResponse:
    """Return current retrieval-related settings."""
    s = get_settings()
    return _retrieval_response(s)


@router.put("/system/retrieval-settings")
async def update_retrieval_settings(
    body: RetrievalSettingsUpdate,
    admin: Principal = Depends(require_admin),
) -> RetrievalSettingsResponse:
    """Update retrieval-related settings (mutates singleton, syncs native config)."""
    s = get_settings()
    for key in body.model_fields_set:
        value = getattr(body, key)
        if value is not None:
            setattr(s, key, value)
            sync_native_config(key, value)

    return _retrieval_response(s)


def _retrieval_response(s) -> RetrievalSettingsResponse:
    return RetrievalSettingsResponse(
        max_history_messages=s.max_history_messages,
        mcp_max_k=s.mcp_max_k,
        mcp_brief_chars=s.mcp_brief_chars,
        chat_search_paginated=s.chat_search_paginated,
        chat_search_k=s.chat_search_k,
        research_search_paginated=s.research_search_paginated,
        research_search_k=s.research_search_k,
    )


class RateLimitSettingsUpdate(BaseModel):
    default_rate_limit_rpm: int | None = None
    default_rate_limit_rph: int | None = None

    @field_validator("default_rate_limit_rpm", "default_rate_limit_rph")
    @classmethod
    def validate_min(cls, v):
        if v is not None and v < 1:
            raise ValueError("must be >= 1")
        return v


@router.get("/system/rate-limit-settings")
async def get_rate_limit_settings(
    admin: Principal = Depends(require_admin),
):
    s = get_settings()
    return {
        "default_rate_limit_rpm": s.default_rate_limit_rpm,
        "default_rate_limit_rph": s.default_rate_limit_rph,
    }


@router.put("/system/rate-limit-settings")
async def update_rate_limit_settings(
    body: RateLimitSettingsUpdate,
    admin: Principal = Depends(require_admin),
):
    s = get_settings()
    if body.default_rate_limit_rpm is not None:
        s.default_rate_limit_rpm = body.default_rate_limit_rpm
        sync_native_config("default_rate_limit_rpm", body.default_rate_limit_rpm)
    if body.default_rate_limit_rph is not None:
        s.default_rate_limit_rph = body.default_rate_limit_rph
        sync_native_config("default_rate_limit_rph", body.default_rate_limit_rph)
    return {
        "default_rate_limit_rpm": s.default_rate_limit_rpm,
        "default_rate_limit_rph": s.default_rate_limit_rph,
    }


@router.get("/system/logs")
async def list_logs(
    admin: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """List service log files with sizes and paths."""
    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if not config_file:
        return {"mode": "docker", "logs_dir": None, "files": []}

    logs_dir = Path(config_file).parent / "logs"
    if not logs_dir.is_dir():
        return {"mode": "native", "logs_dir": str(logs_dir), "files": []}

    service_labels = {
        "api": "API Server",
        "worker": "Worker",
        "embedder": "Embedder",
        "reranker": "Reranker",
        "postgres": "PostgreSQL",
    }

    files = []
    for p in sorted(logs_dir.iterdir()):
        if not p.is_file() or not p.name.endswith(".log"):
            continue
        stat = p.stat()
        # Match service from filename (e.g. "api.log", "worker-io.log", "postgres-Mon.log")
        stem = p.stem.split("-")[0] if "-" in p.stem else p.stem
        label = service_labels.get(stem, stem.title())
        if stem == "worker" and "-io" in p.name:
            label = "Worker (IO)"
        elif stem == "worker" and "-cpu" in p.name:
            label = "Worker (CPU)"
        files.append(
            {
                "name": p.name,
                "path": str(p),
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "service": label,
            }
        )

    return {"mode": "native", "logs_dir": str(logs_dir), "files": files}


@router.get("/system/summary-backlog")
async def get_summary_backlog(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Stats for the Observatory Summary Backlog widget.

    - queue_depth: count of summarize jobs currently queued or running
    - throughput_per_min: completed summarize jobs / minute over the
      last 10 minutes
    - p50_seconds: median wall time of summarize jobs completed in the
      last 100 jobs
    - depth_history: [(unix_ts, depth), ...] sampled every 5 minutes
      over the last hour for the trend sparkline
    """
    now = datetime.now(UTC)

    queue_depth = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status.in_((JobStatus.queued, JobStatus.running)))
        )
    ).scalar() or 0

    ten_min_ago = now - timedelta(minutes=10)
    completed_in_window = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.stage == JobStage.summarize)
            .where(IngestionJob.status == JobStatus.done)
            .where(IngestionJob.finished_at >= ten_min_ago)
        )
    ).scalar() or 0
    throughput_per_min = round(completed_in_window / 10.0, 2)

    # p50 over the last 100 completed summarize jobs
    durations = (
        (
            await session.execute(
                select(func.extract("epoch", IngestionJob.finished_at - IngestionJob.started_at).label("dur"))
                .where(IngestionJob.stage == JobStage.summarize)
                .where(IngestionJob.status == JobStatus.done)
                .where(IngestionJob.started_at.is_not(None))
                .where(IngestionJob.finished_at.is_not(None))
                .order_by(IngestionJob.finished_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    if durations:
        sorted_d = sorted(float(d) for d in durations if d is not None)
        p50 = sorted_d[len(sorted_d) // 2] if sorted_d else 0.0
    else:
        p50 = 0.0

    # Depth history: sample every 5 minutes over the last hour, in a
    # single query using generate_series + LEFT JOIN so the endpoint
    # stays at one DB roundtrip instead of 13. Use CAST(...) rather
    # than ::timestamptz because SQLAlchemy's bind-parameter syntax
    # (`:start_ts`) collides with PostgreSQL's `::` cast operator.
    hour_ago = now - timedelta(minutes=60)
    result = await session.execute(
        text(
            """
            SELECT gs.ts AS ts,
                   COUNT(j.job_id) AS depth
            FROM generate_series(
                CAST(:start_ts AS timestamptz),
                CAST(:end_ts AS timestamptz),
                interval '5 minutes'
            ) AS gs(ts)
            LEFT JOIN ingestion_jobs j
                ON j.stage = 'summarize'
               AND j.created_at <= gs.ts
               AND (j.finished_at IS NULL OR j.finished_at > gs.ts)
            GROUP BY gs.ts
            ORDER BY gs.ts
            """
        ),
        {"start_ts": hour_ago, "end_ts": now},
    )
    depth_history: list[tuple[float, int]] = [(row.ts.timestamp(), int(row.depth)) for row in result]

    return {
        "queue_depth": int(queue_depth),
        "throughput_per_min": float(throughput_per_min),
        "p50_seconds": float(round(p50, 1)),
        "depth_history": depth_history,
    }
