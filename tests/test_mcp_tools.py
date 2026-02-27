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
    kb_search,
)
from harbor_clerk.search import SearchHit, SearchResult


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


# ---------------------------------------------------------------------------
# kb_search — filter and faceted tests (mock hybrid_search)
# ---------------------------------------------------------------------------


def _make_hit(
    doc_id="d1", doc_title="Doc 1", chunk_id=None, score=1.0, language="english"
):
    """Helper to build a SearchHit."""
    return SearchHit(
        chunk_id=chunk_id or str(uuid.uuid4()),
        doc_id=doc_id,
        version_id=str(uuid.uuid4()),
        chunk_num=0,
        chunk_text="some text",
        page_start=1,
        page_end=1,
        language=language,
        ocr_used=False,
        ocr_confidence=None,
        score=score,
        doc_title=doc_title,
    )


@pytest.fixture
def mock_hybrid_search(monkeypatch):
    """Provide a mock for hybrid_search that captures kwargs and returns configurable results."""
    captured = {}
    result_override = SearchResult(hits=[], total_candidates=0)

    async def _mock(
        session,
        query,
        *,
        k=10,
        doc_id=None,
        version_id=None,
        offset=0,
        doc_ids=None,
        after=None,
        before=None,
        language=None,
        mime_type=None,
    ):
        captured.update(
            query=query,
            k=k,
            doc_id=doc_id,
            version_id=version_id,
            offset=offset,
            doc_ids=doc_ids,
            after=after,
            before=before,
            language=language,
            mime_type=mime_type,
        )
        return result_override

    monkeypatch.setattr("harbor_clerk.mcp_server.hybrid_search", _mock)
    return captured, result_override


def _set_result(result_obj, hits, total=None):
    """Mutate the result_override in-place."""
    result_obj.hits = hits
    result_obj.total_candidates = total if total is not None else len(hits)


async def test_kb_search_doc_ids_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """doc_ids are parsed and passed through."""
    captured, result = mock_hybrid_search
    d1 = str(uuid.uuid4())
    d2 = str(uuid.uuid4())
    await kb_search("test", doc_ids=[d1, d2])
    assert captured["doc_ids"] == [uuid.UUID(d1), uuid.UUID(d2)]


async def test_kb_search_date_filters(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """ISO date strings are parsed to datetime objects."""
    captured, result = mock_hybrid_search
    await kb_search("test", after="2025-01-01T00:00:00+00:00", before="2025-12-31")
    from datetime import datetime, timezone

    assert captured["after"] == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert captured["before"] == datetime(2025, 12, 31)


async def test_kb_search_language_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """language is passed through to hybrid_search."""
    captured, result = mock_hybrid_search
    await kb_search("test", language="french")
    assert captured["language"] == "french"


async def test_kb_search_mime_type_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """mime_type is passed through to hybrid_search."""
    captured, result = mock_hybrid_search
    await kb_search("test", mime_type="application/pdf")
    assert captured["mime_type"] == "application/pdf"


async def test_kb_search_doc_id_doc_ids_mutual_exclusion(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Providing both doc_id and doc_ids returns error JSON."""
    raw = await kb_search("test", doc_id=str(uuid.uuid4()), doc_ids=[str(uuid.uuid4())])
    result = json.loads(raw)
    assert "error" in result
    assert "both" in result["error"].lower()


async def test_kb_search_invalid_date(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Invalid ISO date returns error JSON."""
    raw = await kb_search("test", after="not-a-date")
    result = json.loads(raw)
    assert "error" in result
    assert "after" in result["error"]


async def test_kb_search_faceted_output(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """faceted=True groups hits by doc_id with top_score and hit_count."""
    captured, result_obj = mock_hybrid_search
    d1 = str(uuid.uuid4())
    d2 = str(uuid.uuid4())
    _set_result(
        result_obj,
        [
            _make_hit(doc_id=d1, doc_title="Doc A", score=0.9),
            _make_hit(doc_id=d1, doc_title="Doc A", score=0.7),
            _make_hit(doc_id=d2, doc_title="Doc B", score=0.8),
        ],
        total=3,
    )

    raw = await kb_search("test", faceted=True)
    result = json.loads(raw)

    assert "documents" in result
    assert len(result["documents"]) == 2
    # Sorted by top_score desc → d1 first (0.9)
    assert result["documents"][0]["doc_id"] == d1
    assert result["documents"][0]["top_score"] == 0.9
    assert result["documents"][0]["hit_count"] == 2
    assert result["documents"][1]["doc_id"] == d2
    assert result["documents"][1]["top_score"] == 0.8
    assert result["documents"][1]["hit_count"] == 1


async def test_kb_search_faceted_detail_modes(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search,
):
    """faceted output respects detail modes (brief truncates text)."""
    captured, result_obj = mock_hybrid_search
    hit = _make_hit(doc_id=str(uuid.uuid4()), score=1.0)
    hit.chunk_text = "x" * 500
    _set_result(result_obj, [hit])

    raw = await kb_search("test", faceted=True, detail="brief", brief_chars=50)
    result = json.loads(raw)

    doc = result["documents"][0]
    text = doc["hits"][0]["text"]
    assert len(text) <= 51  # 50 chars + ellipsis
