"""REST API request logging middleware for API key requests.

Uses a pure ASGI middleware (not BaseHTTPMiddleware) to avoid creating
anyio task groups that leak into test teardown on Python 3.12.
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

# Replace UUID-shaped path segments with {id}
_UUID_RE = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _normalize_path(method: str, path: str) -> str:
    """Normalize a request path: 'GET /api/docs/550e8400-...' → 'GET /api/docs/{id}'."""
    normalized = _UUID_RE.sub("/{id}", path)
    return f"{method} {normalized}"


class ApiKeyRequestLogMiddleware:
    """Pure ASGI middleware that logs REST API requests made with API keys.

    Only fires for api_key principals. Skips human JWT users.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        response_status = [200]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 200)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        # Check principal set by deps.py during request handling
        state = scope.get("state", {})
        principal = state.get("principal") if isinstance(state, dict) else getattr(state, "principal", None)
        if principal is None or principal.type != "api_key":
            return

        elapsed = int((time.monotonic() - start) * 1000)
        method = scope.get("method", "GET")
        endpoint = _normalize_path(method, path)
        status_code = response_status[0]
        req_status = "ok" if status_code < 400 else "error"
        status_detail = None if req_status == "ok" else f"HTTP {status_code}"

        # Extract query params
        qs = scope.get("query_string", b"").decode("latin-1")
        parameters = dict(pair.split("=", 1) for pair in qs.split("&") if "=" in pair) if qs else None

        try:
            from harbor_clerk.api.request_log import log_api_request
            from harbor_clerk.db import async_session_factory

            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type="rest",
                    endpoint=endpoint,
                    parameters=parameters,
                    status=req_status,
                    status_detail=status_detail,
                    duration_ms=elapsed,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log REST request", exc_info=True)
