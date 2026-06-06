import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from harbor_clerk.api.routes.api_keys import router as api_keys_router
from harbor_clerk.api.routes.auth import router as auth_router
from harbor_clerk.api.routes.chat import router as chat_router
from harbor_clerk.api.routes.documents import router as documents_router
from harbor_clerk.api.routes.jobs import router as jobs_router
from harbor_clerk.api.routes.languages import router as languages_router
from harbor_clerk.api.routes.mail import router as mail_router
from harbor_clerk.api.routes.oauth import router as oauth_router
from harbor_clerk.api.routes.research import router as research_router
from harbor_clerk.api.routes.search import router as search_router
from harbor_clerk.api.routes.setup import router as setup_router
from harbor_clerk.api.routes.stats import router as stats_router
from harbor_clerk.api.routes.system import router as system_router
from harbor_clerk.api.routes.uploads import router as uploads_router
from harbor_clerk.api.routes.users import router as users_router
from harbor_clerk.api.routes.watch import router as watch_router
from harbor_clerk.config import get_settings
from harbor_clerk.db_health import panic_on_sentinel_mismatch
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)


def _find_alembic_dir() -> Path | None:
    """Walk parents of this file looking for an `alembic/` directory next to
    an `alembic.ini`. Works in dev (src layout: repo root has both) and in
    the bundled macOS app (bundle resource dir has both). Returns None if
    no candidate is found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic"
        if candidate.is_dir() and (parent / "alembic.ini").exists():
            return candidate
    return None


def _get_expected_schema_version() -> str | None:
    """Discover the head migration revision via alembic's ScriptDirectory.

    Returns the revision id string (e.g. "0018") or None if alembic isn't
    importable or the migrations directory can't be located. Called once at
    startup; the result is logged for the schema-version sanity check.

    This replaces the previous pattern of hardcoding `_EXPECTED_SCHEMA_VERSION`
    inside the lifespan function — that constant had to be bumped on every
    new migration, which was easy to forget and produced misleading
    "schema mismatch" log lines after every release. Reading the head
    dynamically keeps the check in sync automatically.
    """
    try:
        from alembic.script import ScriptDirectory
    except ImportError:
        return None

    alembic_dir = _find_alembic_dir()
    if alembic_dir is None:
        return None

    try:
        script = ScriptDirectory(str(alembic_dir))
        return script.get_current_head()
    except Exception:
        logger.exception("Found alembic dir at %s but could not read head", alembic_dir)
        return None


# Create MCP app + session manager at module level so we can wire lifespan
from harbor_clerk.mcp_server import create_mcp_app  # noqa: E402

_mcp_asgi, _mcp_token_asgi, _mcp_session_manager = create_mcp_app()


async def _session_reaper_loop() -> None:
    """Background task: clean up stale sessions and research tasks every 5 minutes."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models import Document, Upload, UploadSession

            async with async_session_factory() as db:
                from sqlalchemy import select

                now = datetime.now(UTC)

                # Cancel sessions still active after 24h
                stale_cutoff = now - timedelta(hours=24)
                result = await db.execute(
                    select(UploadSession).where(
                        UploadSession.status == "active",
                        UploadSession.created_at < stale_cutoff,
                    )
                )
                stale_sessions = result.scalars().all()
                for us in stale_sessions:
                    logger.info("Reaper: cancelling stale session %s (created %s)", us.session_id, us.created_at)
                    us.status = "cancelled"
                    us.updated_at = now

                # Delete temp files for cancelled/completed sessions older than 1h
                cleanup_cutoff = now - timedelta(hours=1)
                result = await db.execute(
                    select(UploadSession).where(
                        UploadSession.status.in_(["cancelled", "completed"]),
                        UploadSession.updated_at < cleanup_cutoff,
                    )
                )
                done_sessions = result.scalars().all()
                storage = get_storage()
                settings = get_settings()
                for us in done_sessions:
                    upload_result = await db.execute(
                        select(Upload).where(
                            Upload.session_id == us.session_id,
                            Upload.minio_object_key.like("tmp/%"),
                        )
                    )
                    for upload in upload_result.scalars().all():
                        try:
                            storage.remove_object(settings.minio_bucket, upload.minio_object_key)
                            upload.minio_object_key = ""
                        except Exception:
                            logger.warning("Reaper: failed to delete %s", upload.minio_object_key)

                # Mark stale research tasks as interrupted (heartbeat >5 min old)
                from harbor_clerk.models.research_state import ResearchState

                research_cutoff = now - timedelta(minutes=5)
                result = await db.execute(
                    select(ResearchState).where(
                        ResearchState.status == "running",
                        (ResearchState.heartbeat_at < research_cutoff) | (ResearchState.heartbeat_at.is_(None)),
                    )
                )
                stale_research = result.scalars().all()
                for rs in stale_research:
                    logger.info(
                        "Reaper: marking stale research task %s as interrupted (heartbeat=%s)",
                        rs.conversation_id,
                        rs.heartbeat_at,
                    )
                    rs.status = "interrupted"
                    rs.error = "Research task stalled — no progress for 5+ minutes"

                # Re-queue orphaned ingestion jobs (running with stale heartbeat >90s)
                from harbor_clerk.models import Document, IngestionJob
                from harbor_clerk.models.enums import JobStatus

                heartbeat_cutoff = now - timedelta(seconds=90)
                orphan_result = await db.execute(
                    select(IngestionJob)
                    .join(Document, Document.doc_id == IngestionJob.doc_id)
                    .where(
                        Document.status == "active",
                        IngestionJob.status == JobStatus.running,
                        IngestionJob.pipeline_seq == Document.pipeline_seq,
                        IngestionJob.heartbeat_at < heartbeat_cutoff,
                    )
                )
                orphan_jobs = orphan_result.scalars().all()
                for job in orphan_jobs:
                    logger.warning(
                        "Reaper: re-queuing orphan job doc=%s stage=%s (heartbeat=%s)",
                        job.doc_id,
                        job.stage.value,
                        job.heartbeat_at,
                    )
                    job.status = JobStatus.queued
                    job.started_at = None
                    job.heartbeat_at = None
                    job.error = None

                await db.commit()
                total = len(stale_sessions) + len(done_sessions)
                if total > 0 or stale_research or orphan_jobs:
                    logger.info(
                        "Session reaper: cancelled %d stale, cleaned %d done, interrupted %d research, re-queued %d orphan jobs",
                        len(stale_sessions),
                        len(done_sessions),
                        len(stale_research),
                        len(orphan_jobs),
                    )

                # Clean up watched files removed >30 days ago
                try:
                    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

                    watch_cutoff = now - timedelta(days=30)
                    expired_result = await db.execute(
                        select(WatchedFile).where(
                            WatchedFile.status == WatchedFileStatus.removed,
                            WatchedFile.removed_at < watch_cutoff,
                        )
                    )
                    expired = expired_result.scalars().all()
                    for wf in expired:
                        if wf.doc_id:
                            doc = await db.get(Document, wf.doc_id)
                            if doc:
                                await db.delete(doc)
                        await db.delete(wf)
                    if expired:
                        logger.info("Reaper: hard-deleted %d expired watched files", len(expired))
                    await db.commit()
                except Exception:
                    # watched_files table may not exist if migration 0011 hasn't run
                    await db.rollback()

                # Purge API request log entries older than 90 days
                try:
                    from sqlalchemy import delete

                    from harbor_clerk.models.api_request_log import ApiRequestLog

                    purge_cutoff = now - timedelta(days=90)
                    purge_result = await db.execute(
                        delete(ApiRequestLog).where(ApiRequestLog.created_at < purge_cutoff)
                    )
                    purge_count = purge_result.rowcount
                    if purge_count:
                        logger.info("Reaper: purged %d expired API request log entries", purge_count)
                    await db.commit()
                except Exception:
                    logger.warning("Reaper: failed to purge old API request logs", exc_info=True)
                    await db.rollback()

                # Refresh topics if corpus changed (runs in warm ProcessPoolExecutor)
                from harbor_clerk.topics import check_and_recompute_topics

                await check_and_recompute_topics(db)
        except Exception:
            logger.exception("Session reaper error")


async def _imap_audit_reaper_loop() -> None:
    """Reap imap_command_log rows older than 30 days, hourly."""
    while True:
        try:
            from harbor_clerk.mail.audit import audit_session_scope, reap_old_imap_command_logs

            async with audit_session_scope() as session:
                deleted = await reap_old_imap_command_logs(session)
                if deleted:
                    logger.info("imap_command_log reaper: deleted %d rows", deleted)
        except Exception:
            logger.exception("imap_command_log reaper failed")
        await asyncio.sleep(3600)  # hourly


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    from harbor_clerk.log_setup import setup_logging

    setup_logging("api", settings.log_level)

    logger.info("Starting Harbor Clerk API")

    # Verify database schema is up to date. The expected head is read from
    # the alembic migrations directory at startup, so this stays correct
    # automatically as new migrations land — no manual constant bumps.
    expected = _get_expected_schema_version()
    try:
        from harbor_clerk.db import async_session_factory

        async with async_session_factory() as db:
            from sqlalchemy import text as sa_text

            result = await db.execute(sa_text("SELECT version_num FROM alembic_version"))
            row = result.first()
            if row is None:
                logger.error("No alembic_version found — database may not be initialized")
            elif expected is None:
                logger.info(
                    "Database schema version: %s (could not auto-detect expected head; check skipped)",
                    row[0],
                )
            elif row[0] != expected:
                logger.error(
                    "Database schema version mismatch: have %s, expected %s. Run migrations.",
                    row[0],
                    expected,
                )
            else:
                logger.info("Database schema version: %s (current)", row[0])
    except Exception:
        logger.exception("Failed to check database schema version")

    from harbor_clerk.db import async_session_factory

    async with async_session_factory() as db:
        await panic_on_sentinel_mismatch(db)

    if settings.secret_key == "change-me-in-production":
        logger.warning("SECRET_KEY is set to the default value. Change it to a random string for production use.")

    # Ensure storage bucket exists
    get_storage().ensure_bucket(settings.minio_bucket)

    # One-shot post-migration rename pass for 0017 flatten — idempotent
    try:
        from harbor_clerk.maintenance.rename_originals import rename_all

        # Quick pre-check: skip the pass if no version-prefixed keys exist.
        # rename_all is idempotent so this is just an optimization.
        backend = get_storage()
        has_version_keys = False
        for _ in backend.list_objects(settings.minio_bucket, prefix="originals/versions/", recursive=True):
            has_version_keys = True
            break
        if has_version_keys:
            from harbor_clerk.db_sync import get_sync_session

            s = get_sync_session()
            try:
                renamed, orphans = rename_all(s)
                logger.info("0017 rename pass: %d renamed, %d orphans deleted", renamed, orphans)
            finally:
                s.close()
        else:
            logger.info("0017 rename pass: no legacy keys found, skipping")
    except Exception:
        logger.exception("0017 rename pass failed — continuing anyway")

    # Warm up BERTopic process pool in background (numba JIT takes ~1 min first time)
    from harbor_clerk.topics import warmup_topic_pool

    warmup_task = asyncio.create_task(warmup_topic_pool())

    # Start session reaper background task
    reaper_task = asyncio.create_task(_session_reaper_loop())

    # Start IMAP audit log reaper background task (hourly, 30-day retention)
    imap_audit_reaper_task = asyncio.create_task(_imap_audit_reaper_loop())

    try:
        # Start MCP session manager (required for Streamable HTTP transport)
        if _mcp_session_manager is not None:
            async with _mcp_session_manager.run():
                yield
        else:
            yield
    finally:
        reaper_task.cancel()
        imap_audit_reaper_task.cancel()
        warmup_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
        try:
            await imap_audit_reaper_task
        except asyncio.CancelledError:
            pass

        # Shut down topic process pool
        from harbor_clerk.topics import _topic_pool

        if _topic_pool is not None:
            _topic_pool.shutdown(wait=False)

    logger.info("Shutting down Harbor Clerk API")


BUILD_HASH = os.environ.get("BUILD_HASH", "dev")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Harbor Clerk",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def add_build_hash(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Build-Hash"] = BUILD_HASH
        return response

    # REST request logging middleware — only added when not running under test.
    # In test environments, the ASGI test client + session-scoped engine fixture
    # causes event loop mismatches in Python 3.12 CI.
    import os

    if "PYTEST_CURRENT_TEST" not in os.environ:
        from harbor_clerk.api.middleware import ApiKeyRequestLogMiddleware

        app.add_middleware(ApiKeyRequestLogMiddleware)

    app.include_router(system_router, prefix="/api")
    app.include_router(setup_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(users_router, prefix="/api")
    app.include_router(api_keys_router, prefix="/api")
    app.include_router(uploads_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(stats_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(research_router, prefix="/api")
    app.include_router(watch_router, prefix="/api")
    app.include_router(languages_router, prefix="/api")
    app.include_router(mail_router, prefix="/api")

    # OAuth 2.1 endpoints (no /api prefix — /.well-known must be at root)
    app.include_router(oauth_router)

    # Mount MCP Streamable HTTP endpoints
    app.mount("/mcp", _mcp_asgi)  # Header-based auth (Authorization: Bearer <key>)
    app.mount("/t", _mcp_token_asgi)  # URL-token auth for authless MCP clients (/t/<api_key>)

    # Serve SPA static files (must be last — catches all unmatched routes)
    settings = get_settings()
    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        # Serve actual static assets (JS, CSS, images)
        app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")

        # SPA fallback: serve index.html for all non-API, non-asset routes
        index_html = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            # Try to serve a real file first (favicon.ico, etc.)
            file = static_dir / full_path
            if full_path and file.is_file():
                return FileResponse(file)
            return FileResponse(index_html)

    return app


app = create_app()


def main():
    """Entry point for harbor-clerk-api script."""
    settings = get_settings()
    uvicorn.run(
        "harbor_clerk.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
