"""REST API request logging middleware for API key requests."""

import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Replace UUID-shaped path segments with {id}
_UUID_RE = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _normalize_path(method: str, path: str) -> str:
    """Normalize a request path: 'GET /api/docs/550e8400-...' → 'GET /api/docs/{id}'."""
    normalized = _UUID_RE.sub("/{id}", path)
    return f"{method} {normalized}"


class ApiKeyRequestLogMiddleware(BaseHTTPMiddleware):
    """Log REST API requests made with API keys.

    Only fires for api_key principals. Skips human JWT users.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)

        # Only log for API key principals
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.type != "api_key":
            return response

        # Skip non-API paths (static files, MCP, etc.)
        if not request.url.path.startswith("/api/"):
            return response

        # Skip when dependency overrides are active (test environment) — the
        # app's async_session_factory uses a different engine/event loop than
        # the test fixtures, causing "attached to a different loop" errors.
        app = request.app
        if getattr(app, "dependency_overrides", None):
            return response

        elapsed = int((time.monotonic() - start) * 1000)
        endpoint = _normalize_path(request.method, request.url.path)
        status = "ok" if response.status_code < 400 else "error"
        status_detail = None if status == "ok" else f"HTTP {response.status_code}"

        try:
            from harbor_clerk.api.request_log import log_api_request
            from harbor_clerk.db import async_session_factory

            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="rest",
                    endpoint=endpoint,
                    parameters=dict(request.query_params) if request.query_params else None,
                    status=status,
                    status_detail=status_detail,
                    duration_ms=elapsed,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log REST request", exc_info=True)

        return response
