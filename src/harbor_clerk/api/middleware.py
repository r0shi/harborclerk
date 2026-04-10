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

        method = scope.get("method", "GET")
        endpoint = _normalize_path(method, path)

        # Extract query params
        qs = scope.get("query_string", b"").decode("latin-1")
        parameters = dict(pair.split("=", 1) for pair in qs.split("&") if "=" in pair) if qs else None

        # --- Resolve API key and check rate limit BEFORE forwarding ---
        key_id = None
        try:
            from harbor_clerk.auth import hash_api_key
            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models import ApiKey

            key_hash = hash_api_key(token)
            async with async_session_factory() as pre_session:
                from sqlalchemy import select

                row = (
                    await pre_session.execute(
                        select(ApiKey.key_id, ApiKey.rate_limit_rpm, ApiKey.rate_limit_rph).where(
                            ApiKey.key_hash == key_hash
                        )
                    )
                ).one_or_none()
                if row is None:
                    # Invalid key — let auth handler reject it
                    await self.app(scope, receive, send)
                    return

                key_id = row.key_id
                rl_rpm = row.rate_limit_rpm
                rl_rph = row.rate_limit_rph

            # Rate limit check
            from harbor_clerk.api.rate_limiter import rate_limiter
            from harbor_clerk.config import get_settings as _get_settings

            _s = _get_settings()
            rpm = rl_rpm if rl_rpm is not None else _s.default_rate_limit_rpm
            rph = rl_rph if rl_rph is not None else _s.default_rate_limit_rph
            allowed, retry_after = await rate_limiter.check(key_id, rpm, rph)
            if not allowed:
                logger.warning(
                    "Rate limit exceeded for key %s on REST %s (retry_after=%ds)",
                    key_id,
                    endpoint,
                    retry_after,
                )
                # Log the rate-limited request
                try:
                    from harbor_clerk.api.request_log import log_api_request

                    async with async_session_factory() as log_session:
                        await log_api_request(
                            log_session,
                            api_key_id=key_id,
                            request_type="rest",
                            endpoint=endpoint,
                            parameters=parameters,
                            status="rate_limited",
                            status_detail=f"retry_after={retry_after}s",
                            duration_ms=0,
                        )
                        await log_session.commit()
                except Exception:
                    logger.debug("Failed to log rate-limited REST request", exc_info=True)

                # Send 429 response
                import json as _json

                body = _json.dumps({"detail": "Rate limit exceeded", "retry_after": retry_after}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                            [b"retry-after", str(retry_after).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        except Exception:
            logger.debug("Failed to check rate limit for REST request", exc_info=True)

        # --- Forward request to app ---
        start = time.monotonic()
        response_status = [200]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 200)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        elapsed = int((time.monotonic() - start) * 1000)
        status_code = response_status[0]
        req_status = "ok" if status_code < 400 else "error"
        status_detail = None if req_status == "ok" else f"HTTP {status_code}"

        # --- Log the request ---
        if key_id is not None:
            try:
                from harbor_clerk.api.request_log import log_api_request
                from harbor_clerk.db import async_session_factory

                async with async_session_factory() as log_session:
                    await log_api_request(
                        log_session,
                        api_key_id=key_id,
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
