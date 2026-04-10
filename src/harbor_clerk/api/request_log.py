"""API request logging helper — writes per-request telemetry to api_request_log."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.api_request_log import ApiRequestLog


async def log_api_request(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID | None,
    request_type: str,
    endpoint: str,
    parameters: dict[str, Any] | None = None,
    status: str = "ok",
    status_detail: str | None = None,
    result_summary: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> None:
    """Insert a single request log entry. Caller must commit."""
    entry = ApiRequestLog(
        api_key_id=api_key_id,
        request_type=request_type,
        endpoint=endpoint,
        parameters=parameters,
        status=status,
        status_detail=status_detail,
        result_summary=result_summary,
        duration_ms=duration_ms,
    )
    session.add(entry)
