import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.request_log import log_api_request
from harbor_clerk.models.api_request_log import ApiRequestLog


@pytest.mark.anyio
async def test_create_request_log_entry(db_session: AsyncSession):
    """Basic insert and read-back."""
    entry = ApiRequestLog(
        api_key_id=None,
        request_type="mcp_tool",
        endpoint="kb_search",
        parameters={"query": "test", "k": 10},
        status="ok",
        result_summary={"count": 5},
        duration_ms=42,
    )
    db_session.add(entry)
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.request_id == entry.request_id))
    row = result.scalar_one()
    assert row.endpoint == "kb_search"
    assert row.status == "ok"
    assert row.parameters["query"] == "test"
    assert row.duration_ms == 42


@pytest.mark.anyio
async def test_log_api_request_helper(db_session: AsyncSession):
    """log_api_request creates an entry with all fields."""
    await log_api_request(
        db_session,
        api_key_id=None,
        request_type="mcp_tool",
        endpoint="kb_search",
        parameters={"query": "budget"},
        status="ok",
        result_summary={"count": 3},
        duration_ms=55,
    )
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.endpoint == "kb_search"))
    row = result.scalar_one()
    assert row.api_key_id is None
    assert row.duration_ms == 55


@pytest.mark.anyio
async def test_log_api_request_denied(db_session: AsyncSession):
    """Denied requests are logged with status_detail."""
    await log_api_request(
        db_session,
        api_key_id=None,
        request_type="mcp_tool",
        endpoint="kb_reprocess",
        status="denied",
        status_detail="tool not in search tier",
        duration_ms=0,
    )
    await db_session.flush()

    result = await db_session.execute(select(ApiRequestLog).where(ApiRequestLog.status == "denied"))
    row = result.scalar_one()
    assert row.status_detail == "tool not in search tier"
