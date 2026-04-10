"""REST API request logging middleware for API key requests.

Uses a pure ASGI middleware (not BaseHTTPMiddleware) to avoid creating
anyio task groups that leak into test teardown on Python 3.12.

Detects API key requests from the Authorization header (hc_/lka_ prefix)
rather than relying on request.state, so deps.py doesn't need modification.
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

# Replace UUID-shaped path segments with {id}
_UUID_RE = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

# API key prefixes (must match harbor_clerk.auth)
_API_KEY_PREFIXES = ("hc_", "lka_")


def _normalize_path(method: str, path: str) -> str:
    """Normalize a request path: 'GET /api/docs/550e8400-...' → 'GET /api/docs/{id}'."""
    normalized = _UUID_RE.sub("/{id}", path)
    return f"{method} {normalized}"


def _extract_api_key_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract API key token from Authorization header, or None if not an API key."""
    for name, value in headers:
        if name == b"authorization":
            decoded = value.decode("latin-1")
            if decoded.startswith("Bearer "):
                token = decoded[7:]
                if token.startswith(_API_KEY_PREFIXES):
                    return token
            break
    return None


class ApiKeyRequestLogMiddleware:
    """Pure ASGI middleware that logs REST API requests made with API keys.

    Detects API key auth from the Authorization header prefix.
    Only fires for /api/ paths. Skips JWT users entirely.
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

        # Quick check: is this an API key request?
        token = _extract_api_key_token(scope.get("headers", []))
        if token is None:
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        response_status = [200]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 200)
            await send(message)

        await self.app(scope, receive, send_wrapper)

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
            from harbor_clerk.auth import hash_api_key
            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models import ApiKey

            # Resolve the key_id from the token hash
            key_hash = hash_api_key(token)
            async with async_session_factory() as log_session:
                from sqlalchemy import select

                row = (
                    await log_session.execute(select(ApiKey.key_id).where(ApiKey.key_hash == key_hash))
                ).scalar_one_or_none()
                if row is None:
                    return  # invalid key, auth handler already rejected it

                await log_api_request(
                    log_session,
                    api_key_id=row,
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
