"""Tests for MCP server tools (read-only, no embedder required)."""

import json
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harbor_clerk.api.deps import Principal
from harbor_clerk.models import Chunk, Document, DocumentVersion
from harbor_clerk.models.enums import VersionStatus
from harbor_clerk.mcp_server import (
    _mcp_principal,
    kb_corpus_overview,
    kb_expand_context,
    kb_get_document,
    kb_list_recent,
    kb_read_passages,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_principal(admin_user):
    """Set _mcp_principal context var to an admin Principal for the test."""
    token = _mcp_principal.set(
        Principal(type="user", id=admin_user.user_id, role="admin")
    )
    yield
    _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to create proper AsyncSessions sharing the
    test connection, so lazy loading (greenlet-based) works correctly."""
    # Get the connection backing db_session so new sessions see flushed data.
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.fixture
async def sample_doc(db_session: AsyncSession):
    """Create a Document + DocumentVersion (status=ready)."""
    doc = Document(title="Test Document", status="active")
    db_session.add(doc)
    await db_session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=b"fake_sha256_for_testing_1234567",
        original_bucket="originals",
        original_object_key=f"originals/versions/{uuid.uuid4()}/test.pdf",
        mime_type="application/pdf",
        size_bytes=12345,
        status=VersionStatus.ready,
        source_path="test.pdf",
        summary="A test document summary.",
    )
    db_session.add(version)
    await db_session.flush()

    doc.latest_version_id = version.version_id
    await db_session.flush()

    return doc, version


@pytest.fixture
async def sample_chunks(db_session: AsyncSession, sample_doc):
    """Create 5 chunks (chunk_num 0–4) for the sample document."""
    doc, version = sample_doc
    chunks = []
    for i in range(5):
        chunk = Chunk(
            version_id=version.version_id,
            doc_id=doc.doc_id,
            chunk_num=i,
            page_start=i + 1,
            page_end=i + 1,
            char_start=i * 1000,
            char_end=(i + 1) * 1000,
            chunk_text=f"Chunk {i} text content for testing.",
            language="en",
        )
        db_session.add(chunk)
        chunks.append(chunk)
    await db_session.flush()
    return chunks


# ---------------------------------------------------------------------------
# kb_expand_context
# ---------------------------------------------------------------------------


async def test_expand_context_middle(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Expanding around chunk 2 with n=1 returns chunks 1, 2, 3."""
    result = json.loads(await kb_expand_context(str(sample_chunks[2].chunk_id), n=1))
    assert len(result["chunks"]) == 3
    assert result["chunks"][0]["chunk_num"] == 1
    assert result["chunks"][1]["is_target"] is True
    assert result["chunks"][2]["chunk_num"] == 3


async def test_expand_context_start_boundary(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Expanding around chunk 0 with n=2 returns chunks 0, 1, 2 (no negatives)."""
    result = json.loads(await kb_expand_context(str(sample_chunks[0].chunk_id), n=2))
    nums = [c["chunk_num"] for c in result["chunks"]]
    assert nums == [0, 1, 2]
    assert result["chunks"][0]["is_target"] is True


async def test_expand_context_end_boundary(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Expanding around last chunk (4) with n=2 returns chunks 2, 3, 4."""
    result = json.loads(await kb_expand_context(str(sample_chunks[4].chunk_id), n=2))
    nums = [c["chunk_num"] for c in result["chunks"]]
    assert nums == [2, 3, 4]
    assert result["chunks"][2]["is_target"] is True


async def test_expand_context_custom_n(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """n=4 around chunk 2 returns all 5 chunks."""
    result = json.loads(await kb_expand_context(str(sample_chunks[2].chunk_id), n=4))
    assert len(result["chunks"]) == 5


async def test_expand_context_invalid_chunk(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Non-existent chunk_id returns error."""
    result = json.loads(await kb_expand_context(str(uuid.uuid4())))
    assert "error" in result


# ---------------------------------------------------------------------------
# kb_read_passages
# ---------------------------------------------------------------------------


async def test_read_passages_single(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Reading a single passage returns its text and metadata."""
    result = json.loads(await kb_read_passages([str(sample_chunks[0].chunk_id)]))
    assert len(result["passages"]) == 1
    p = result["passages"][0]
    assert p["text"] == "Chunk 0 text content for testing."
    assert p["doc_title"] == "Test Document"
    assert p["pages"] == "1"


async def test_read_passages_multiple(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Reading multiple passages returns them in requested order."""
    ids = [str(sample_chunks[i].chunk_id) for i in [3, 1]]
    result = json.loads(await kb_read_passages(ids))
    assert len(result["passages"]) == 2
    assert result["passages"][0]["text"] == "Chunk 3 text content for testing."
    assert result["passages"][1]["text"] == "Chunk 1 text content for testing."


async def test_read_passages_with_context(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """include_context=True adds context_before and context_after."""
    result = json.loads(
        await kb_read_passages(
            [str(sample_chunks[2].chunk_id)],
            include_context=True,
        )
    )
    p = result["passages"][0]
    assert p["context_before"] == "Chunk 1 text content for testing."
    assert p["context_after"] == "Chunk 3 text content for testing."


async def test_read_passages_missing_skipped(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Non-existent chunk_id is silently skipped."""
    ids = [str(sample_chunks[0].chunk_id), str(uuid.uuid4())]
    result = json.loads(await kb_read_passages(ids))
    assert len(result["passages"]) == 1


# ---------------------------------------------------------------------------
# kb_get_document
# ---------------------------------------------------------------------------


async def test_get_document_happy(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
):
    """Returns document details with version info."""
    doc, version = sample_doc
    result = json.loads(await kb_get_document(str(doc.doc_id)))
    assert result["title"] == "Test Document"
    assert result["latest_version_id"] == str(version.version_id)
    assert len(result["versions"]) == 1
    assert result["versions"][0]["status"] == "ready"
    assert result["versions"][0]["summary"] == "A test document summary."


async def test_get_document_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Non-existent doc_id returns error."""
    result = json.loads(await kb_get_document(str(uuid.uuid4())))
    assert "error" in result


# ---------------------------------------------------------------------------
# kb_list_recent
# ---------------------------------------------------------------------------


async def test_list_recent_returns_docs(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
):
    """Returns active documents."""
    result = json.loads(await kb_list_recent())
    assert len(result["documents"]) == 1
    assert result["documents"][0]["title"] == "Test Document"
    assert result["documents"][0]["latest_version_status"] == "ready"


async def test_list_recent_limit(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Limit parameter is respected."""
    # Create 3 documents
    for i in range(3):
        doc = Document(title=f"Doc {i}", status="active")
        db_session.add(doc)
    await db_session.flush()

    result = json.loads(await kb_list_recent(limit=2))
    assert len(result["documents"]) == 2


# ---------------------------------------------------------------------------
# kb_corpus_overview
# ---------------------------------------------------------------------------


async def test_corpus_overview(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Returns correct counts and document list."""
    result = json.loads(await kb_corpus_overview())
    assert result["document_count"] == 1
    assert result["total_chunks"] == 5
    assert len(result["documents"]) == 1
    assert result["documents"][0]["title"] == "Test Document"
    assert result["documents"][0]["summary"] == "A test document summary."
    assert result["truncated"] is False


async def test_corpus_overview_empty(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Empty corpus returns zeros."""
    result = json.loads(await kb_corpus_overview())
    assert result["document_count"] == 0
    assert result["total_chunks"] == 0
    assert result["documents"] == []
