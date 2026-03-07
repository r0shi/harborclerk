import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
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
    DocumentVersion,
    IngestionJob,
    User,
)
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
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

    from harbor_clerk.api.app import BUILD_HASH

    overall = all(v == "ok" for v in checks.values())
    return {
        "status": "healthy" if overall else "degraded",
        "build": BUILD_HASH,
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
        # Load versions for storage cleanup
        versions_result = await session.execute(select(DocumentVersion).where(DocumentVersion.doc_id == doc.doc_id))
        versions = versions_result.scalars().all()

        for ver in versions:
            # Delete stored object
            try:
                storage.remove_object(ver.original_bucket, ver.original_object_key)
            except Exception as e:
                logger.warning(
                    "Failed to delete object %s/%s: %s",
                    ver.original_bucket,
                    ver.original_object_key,
                    e,
                )

            # Cascade delete DB rows: chunks, pages, ingestion_jobs
            await session.execute(delete(Chunk).where(Chunk.version_id == ver.version_id))
            await session.execute(delete(DocumentPage).where(DocumentPage.version_id == ver.version_id))
            await session.execute(delete(IngestionJob).where(IngestionJob.version_id == ver.version_id))

        # Delete versions and document
        await session.execute(delete(DocumentVersion).where(DocumentVersion.doc_id == doc.doc_id))
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
            "Reaping orphan job: version=%s stage=%s heartbeat_at=%s",
            job.version_id,
            job.stage.value,
            job.heartbeat_at,
        )
        orphans.append((job.version_id, job.stage))

    # Commit any pending state and log audit before re-enqueuing
    await log_audit(
        session,
        user_id=admin.id,
        action="reaper_run",
        detail={"reaped_count": len(orphans)},
    )
    await session.commit()

    # Re-enqueue orphans (each creates its own sync session)
    for version_id, stage in orphans:
        enqueue_stage(version_id, stage)

    logger.info("Reaped %d orphan jobs", len(orphans))
    return {"reaped": len(orphans)}


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
        version_id = doc.latest_version_id
        if version_id is None:
            continue

        ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id))
        version = ver_result.scalar_one_or_none()
        if version is None:
            continue

        version.status = VersionStatus.queued
        version.error = None
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
        if doc.latest_version_id is None:
            continue
        reset_jobs(doc.latest_version_id)
        enqueue_stage(doc.latest_version_id, JobStage.extract)

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

    # Find active documents with ready versions that have chunks
    result = await session.execute(
        select(DocumentVersion.version_id)
        .join(Document, Document.latest_version_id == DocumentVersion.version_id)
        .where(
            Document.status == "active",
            DocumentVersion.status == VersionStatus.ready,
        )
    )
    version_ids = [row[0] for row in result.all()]

    await log_audit(
        session,
        user_id=admin.id,
        action="resummarize_all",
        detail={"resummarized_count": len(version_ids)},
    )
    await session.commit()

    # Enqueue summarize jobs outside the async session (sync calls)
    count = 0
    for vid in version_ids:
        enqueue_stage(vid, JobStage.summarize)
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
        text(
            "TRUNCATE entities, chunks, document_headings, document_pages, ingestion_jobs, document_versions, documents, uploads CASCADE"
        )
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
    return RetrievalSettingsResponse(
        rag_auto_k=s.rag_auto_k,
        rag_auto_threshold=s.rag_auto_threshold,
        max_tool_rounds=s.max_tool_rounds,
        max_history_messages=s.max_history_messages,
        mcp_max_k=s.mcp_max_k,
        mcp_brief_chars=s.mcp_brief_chars,
    )


@router.put("/system/retrieval-settings")
async def update_retrieval_settings(
    body: RetrievalSettingsUpdate,
    admin: Principal = Depends(require_admin),
) -> RetrievalSettingsResponse:
    """Update retrieval-related settings (mutates singleton, syncs native config)."""
    s = get_settings()
    for key in body.model_fields_set:
        value = getattr(body, key)
        setattr(s, key, value)
        sync_native_config(key, str(value))

    return RetrievalSettingsResponse(
        rag_auto_k=s.rag_auto_k,
        rag_auto_threshold=s.rag_auto_threshold,
        max_tool_rounds=s.max_tool_rounds,
        max_history_messages=s.max_history_messages,
        mcp_max_k=s.mcp_max_k,
        mcp_brief_chars=s.mcp_brief_chars,
    )


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
