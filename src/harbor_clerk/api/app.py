import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from harbor_clerk.config import get_settings
from harbor_clerk.storage import get_storage
from harbor_clerk.api.routes.api_keys import router as api_keys_router
from harbor_clerk.api.routes.auth import router as auth_router
from harbor_clerk.api.routes.documents import router as documents_router
from harbor_clerk.api.routes.jobs import router as jobs_router
from harbor_clerk.api.routes.search import router as search_router
from harbor_clerk.api.routes.setup import router as setup_router
from harbor_clerk.api.routes.system import router as system_router
from harbor_clerk.api.routes.uploads import router as uploads_router
from harbor_clerk.api.routes.users import router as users_router

logger = logging.getLogger(__name__)

# Create MCP app + session manager at module level so we can wire lifespan
from harbor_clerk.mcp_server import create_mcp_app  # noqa: E402

_mcp_asgi, _mcp_session_manager = create_mcp_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("Starting Harbor Clerk API")

    # Ensure storage bucket exists
    get_storage().ensure_bucket(settings.minio_bucket)

    # Start MCP session manager (required for Streamable HTTP transport)
    if _mcp_session_manager is not None:
        async with _mcp_session_manager.run():
            yield
    else:
        yield

    logger.info("Shutting down Harbor Clerk API")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Harbor Clerk",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(system_router, prefix="/api")
    app.include_router(setup_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(users_router, prefix="/api")
    app.include_router(api_keys_router, prefix="/api")
    app.include_router(uploads_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(search_router, prefix="/api")

    # Mount MCP Streamable HTTP endpoint
    app.mount("/mcp", _mcp_asgi)

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
