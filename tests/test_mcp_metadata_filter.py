"""MCP wiring for kb_get_document metadata field.

The kb_search metadata_filter param's behavior is covered by the
tests/test_search_metadata_filter.py suite (Task 7); this file pins the
MCP-layer additions: kb_get_document returns the doc's metadata dict so
models can inspect available filter keys."""

import json
import uuid
from contextlib import asynccontextmanager, contextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import _mcp_principal, kb_get_document
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@contextmanager
def _principal_in_context(user):
    """Set the MCP principal contextvar for the duration of the test."""
    token = _mcp_principal.set(Principal(type="user", id=user.user_id, role="admin"))
    try:
        yield
    finally:
        _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to create proper AsyncSessions sharing the
    test connection, so lazy loading (greenlet-based) works correctly."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


async def _seed_doc(db_session, *, title: str, metadata: dict) -> Document:
    doc = Document(
        title=title,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="some body text", language="english"))
    await db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_kb_get_document_includes_doc_metadata(client, admin_user, db_session, mock_session_factory):
    """kb_get_document response includes the metadata dict so models can
    inspect available filter keys."""
    target = await _seed_doc(
        db_session,
        title="invoice-acme",
        metadata={"sidecar": {"vendor": "Acme", "total_usd": 100}},
    )
    # flush only — commit would invalidate the connection shared with mock_session_factory
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_get_document(doc_id=str(target.doc_id))

    parsed = json.loads(raw)
    assert "metadata" in parsed
    assert parsed["metadata"] == {"sidecar": {"vendor": "Acme", "total_usd": 100}}


async def test_kb_get_document_metadata_field_is_empty_dict_when_unset(
    client, admin_user, db_session, mock_session_factory
):
    """Docs with no metadata extracted yet should report metadata = {} (the
    server default), not None or missing."""
    target = await _seed_doc(db_session, title="no-meta", metadata={})
    # flush only — commit would invalidate the connection shared with mock_session_factory
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_get_document(doc_id=str(target.doc_id))

    parsed = json.loads(raw)
    assert parsed.get("metadata") == {}
