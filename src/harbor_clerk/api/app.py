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
from harbor_clerk.api.routes.search import router as search_router
from harbor_clerk.api.routes.setup import router as setup_router
from harbor_clerk.api.routes.stats import router as stats_router
from harbor_clerk.api.routes.system import router as system_router
from harbor_clerk.api.routes.uploads import router as uploads_router
from harbor_clerk.api.routes.users import router as users_router
from harbor_clerk.config import get_settings
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)

# Create MCP app + session manager at module level so we can wire lifespan
from harbor_clerk.mcp_server import create_mcp_app  # noqa: E402

_mcp_asgi, _mcp_token_asgi, _mcp_session_manager = create_mcp_app()


async def _session_reaper_loop() -> None:
    """Background task: clean up stale upload sessions every 15 minutes."""
    while True:
        await asyncio.sleep(15 * 60)
        try:
            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models import Upload, UploadSession

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

                await db.commit()
                total = len(stale_sessions) + len(done_sessions)
                if total > 0:
                    logger.info(
                        "Session reaper: cancelled %d stale, cleaned %d done", len(stale_sessions), len(done_sessions)
                    )
        except Exception:
            logger.exception("Session reaper error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    from harbor_clerk.log_setup import setup_logging

    setup_logging("api", settings.log_level)

    logger.info("Starting Harbor Clerk API")

    if settings.secret_key == "change-me-in-production":
        logger.warning("SECRET_KEY is set to the default value. Change it to a random string for production use.")

    # Ensure storage bucket exists
    get_storage().ensure_bucket(settings.minio_bucket)

    # Start session reaper background task
    reaper_task = asyncio.create_task(_session_reaper_loop())

    try:
        # Start MCP session manager (required for Streamable HTTP transport)
        if _mcp_session_manager is not None:
            async with _mcp_session_manager.run():
                yield
        else:
            yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass

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
