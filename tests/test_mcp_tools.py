"""Tests for MCP server tools (read-only, no embedder required)."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import (
    _mcp_principal,
    kb_batch_search,
    kb_corpus_overview,
    kb_document_outline,
    kb_entity_cooccurrence,
    kb_entity_overview,
    kb_entity_search,
    kb_expand_context,
    kb_find_related,
    kb_get_document,
    kb_list_recent,
    kb_read_document,
    kb_read_passages,
    kb_search,
)
from harbor_clerk.models import (
    Chunk,
    Document,
    DocumentHeading,
    DocumentPage,
    DocumentVersion,
    Entity,
)
from harbor_clerk.models.enums import VersionStatus
from harbor_clerk.search import SearchHit, SearchResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_principal(admin_user):
    """Set _mcp_principal context var to an admin Principal for the test."""
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
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
    """Returns correct counts, aggregate stats, and document list."""
    result = json.loads(await kb_corpus_overview())
    assert result["document_count"] == 1
    assert result["total_chunks"] == 5
    assert result["total_pages"] == 0  # no DocumentPage rows in fixture
    assert result["languages"] == {"en": 5}
    assert result["mime_types"] == {"application/pdf": 1}
    assert result["date_range"]["oldest"] is not None
    assert result["date_range"]["newest"] is not None
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
    """Empty corpus returns zeros and empty aggregates."""
    result = json.loads(await kb_corpus_overview())
    assert result["document_count"] == 0
    assert result["total_chunks"] == 0
    assert result["total_pages"] == 0
    assert result["languages"] == {}
    assert result["mime_types"] == {}
    assert result["date_range"] == {"oldest": None, "newest": None}
    assert result["documents"] == []


async def test_corpus_overview_multilingual(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
):
    """Language distribution correctly counts per-language chunks."""
    doc, version = sample_doc
    for i, lang in enumerate(["en", "en", "fr", "fr", "fr"]):
        db_session.add(
            Chunk(
                version_id=version.version_id,
                doc_id=doc.doc_id,
                chunk_num=i,
                page_start=1,
                page_end=1,
                char_start=i * 100,
                char_end=(i + 1) * 100,
                chunk_text=f"Chunk {i}",
                language=lang,
            )
        )
    await db_session.flush()

    result = json.loads(await kb_corpus_overview())
    assert result["languages"] == {"fr": 3, "en": 2}


# ---------------------------------------------------------------------------
# kb_find_related
# ---------------------------------------------------------------------------


async def test_find_related_happy(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Returns related documents ranked by embedding similarity."""
    # Create two documents with embeddings
    doc1 = Document(title="Machine Learning Guide", status="active")
    doc2 = Document(title="Deep Learning Intro", status="active")
    doc3 = Document(title="Cooking Recipes", status="active")
    db_session.add_all([doc1, doc2, doc3])
    await db_session.flush()

    # Create versions
    for doc in [doc1, doc2, doc3]:
        ver = DocumentVersion(
            doc_id=doc.doc_id,
            original_sha256=f"sha_{doc.title[:8]}".encode().ljust(31, b"_"),
            original_bucket="originals",
            original_object_key=f"originals/versions/{doc.doc_id}/f.pdf",
            mime_type="application/pdf",
            size_bytes=1000,
            status=VersionStatus.ready,
            source_path="f.pdf",
            summary=f"Summary of {doc.title}",
        )
        db_session.add(ver)
        await db_session.flush()
        doc.latest_version_id = ver.version_id

    await db_session.flush()

    # Embeddings: doc1 and doc2 are similar, doc3 is different
    # 384-dim vectors (matching model dimension) with signal in first 4 dims
    similar_emb = [0.9, 0.1, 0.0, 0.0] + [0.0] * 380
    different_emb = [0.0, 0.0, 0.9, 0.1] + [0.0] * 380

    for _i, doc in enumerate([doc1, doc2, doc3]):
        ver_id = doc.latest_version_id
        emb = similar_emb if doc in (doc1, doc2) else different_emb
        db_session.add(
            Chunk(
                version_id=ver_id,
                doc_id=doc.doc_id,
                chunk_num=0,
                page_start=1,
                page_end=1,
                char_start=0,
                char_end=100,
                chunk_text=f"Chunk for {doc.title}",
                language="en",
                embedding=emb,
            )
        )
    await db_session.flush()

    result = json.loads(await kb_find_related(str(doc1.doc_id)))
    assert len(result["related"]) == 2
    # doc2 (similar) should rank higher than doc3 (different)
    assert result["related"][0]["title"] == "Deep Learning Intro"
    assert result["related"][0]["similarity"] > result["related"][1]["similarity"]


async def test_find_related_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Returns error for nonexistent document."""
    fake_id = str(uuid.uuid4())
    result = json.loads(await kb_find_related(fake_id))
    assert "error" in result


async def test_find_related_no_embeddings(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Returns empty list when document has no embeddings."""
    doc, _ = sample_doc
    result = json.loads(await kb_find_related(str(doc.doc_id)))
    assert result["related"] == []
    assert "No embeddings" in result.get("note", "")


# ---------------------------------------------------------------------------
# kb_search — filter and faceted tests (mock hybrid_search)
# ---------------------------------------------------------------------------


def _make_hit(doc_id="d1", doc_title="Doc 1", chunk_id=None, score=1.0, language="english"):
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
        k=10,
        doc_id=None,
        version_id=None,
        offset=0,
        *,
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
    from datetime import datetime

    assert captured["after"] == datetime(2025, 1, 1, tzinfo=UTC)
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


# ---------------------------------------------------------------------------
# kb_batch_search tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_hybrid_search_multi(monkeypatch):
    """Mock for hybrid_search that captures a list of calls (for batch tests)."""
    calls: list[dict] = []
    result_override = SearchResult(hits=[], total_candidates=0)

    async def _mock(
        session,
        query,
        k=10,
        doc_id=None,
        version_id=None,
        offset=0,
        *,
        doc_ids=None,
        after=None,
        before=None,
        language=None,
        mime_type=None,
    ):
        calls.append(
            dict(
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
        )
        return result_override

    monkeypatch.setattr("harbor_clerk.mcp_server.hybrid_search", _mock)
    return calls, result_override


@pytest.mark.asyncio
async def test_batch_search_happy_path(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search_multi,
):
    """Two queries return two grouped results."""
    calls, result_obj = mock_hybrid_search_multi
    hit = _make_hit()
    _set_result(result_obj, [hit])

    raw = await kb_batch_search(queries=["foo", "bar"])
    result = json.loads(raw)

    assert "results" in result
    assert len(result["results"]) == 2
    assert result["results"][0]["query"] == "foo"
    assert result["results"][1]["query"] == "bar"
    assert len(result["results"][0]["hits"]) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_batch_search_max_queries(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search_multi,
):
    """Five queries (the maximum) all succeed."""
    calls, result_obj = mock_hybrid_search_multi
    _set_result(result_obj, [_make_hit()])

    raw = await kb_batch_search(queries=["a", "b", "c", "d", "e"])
    result = json.loads(raw)

    assert len(result["results"]) == 5
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_batch_search_too_many_queries(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Six queries return an error."""
    raw = await kb_batch_search(queries=["a"] * 6)
    result = json.loads(raw)

    assert "error" in result
    assert "max 5" in result["error"]


@pytest.mark.asyncio
async def test_batch_search_empty_queries(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Empty query list returns an error."""
    raw = await kb_batch_search(queries=[])
    result = json.loads(raw)

    assert "error" in result
    assert "at least 1" in result["error"]


@pytest.mark.asyncio
async def test_batch_search_shared_filters(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search_multi,
):
    """Shared filters are passed to every hybrid_search call."""
    calls, result_obj = mock_hybrid_search_multi
    _set_result(result_obj, [])

    await kb_batch_search(queries=["q1", "q2"], language="french")

    assert len(calls) == 2
    assert calls[0]["language"] == "french"
    assert calls[1]["language"] == "french"


@pytest.mark.asyncio
async def test_batch_search_detail_mode(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    mock_hybrid_search_multi,
):
    """Brief detail mode truncates text in batch results."""
    calls, result_obj = mock_hybrid_search_multi
    hit = _make_hit()
    hit.chunk_text = "x" * 500
    _set_result(result_obj, [hit])

    raw = await kb_batch_search(queries=["q1"], detail="brief", brief_chars=10)
    result = json.loads(raw)

    text = result["results"][0]["hits"][0]["text"]
    assert len(text) <= 11  # 10 chars + ellipsis


@pytest.mark.asyncio
async def test_batch_search_invalid_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Invalid date filter returns an error."""
    raw = await kb_batch_search(queries=["q1"], after="bad-date")
    result = json.loads(raw)

    assert "error" in result
    assert "Invalid ISO datetime" in result["error"]


# ---------------------------------------------------------------------------
# kb_document_outline
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_headings(db_session: AsyncSession, sample_doc):
    """Create headings for the sample document's version."""
    _, version = sample_doc
    headings = []
    for i, (level, title) in enumerate([(1, "Introduction"), (2, "Background"), (2, "Methods"), (1, "Results")]):
        h = DocumentHeading(
            version_id=version.version_id,
            level=level,
            title=title,
            page_num=i + 1,
            position=i * 500,
        )
        db_session.add(h)
        headings.append(h)
    await db_session.flush()
    return headings


@pytest.fixture
async def sample_pages(db_session: AsyncSession, sample_doc):
    """Create pages for the sample document's version."""
    _, version = sample_doc
    pages = []
    for i in range(3):
        p = DocumentPage(
            version_id=version.version_id,
            page_num=i + 1,
            page_text=f"Page {i + 1} content.",
            ocr_used=False,
        )
        db_session.add(p)
        pages.append(p)
    await db_session.flush()
    return pages


async def test_document_outline_happy(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_headings,
    sample_pages,
    sample_chunks,
):
    """Outline returns headings, page count, and chunk count."""
    doc, version = sample_doc
    result = json.loads(await kb_document_outline(str(doc.doc_id)))
    assert result["doc_id"] == str(doc.doc_id)
    assert result["version_id"] == str(version.version_id)
    assert result["title"] == "Test Document"
    assert result["page_count"] == 3
    assert result["chunk_count"] == 5
    assert len(result["headings"]) == 4
    assert result["headings"][0] == {
        "level": 1,
        "title": "Introduction",
        "page_num": 1,
    }
    assert result["headings"][3]["title"] == "Results"


async def test_document_outline_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Non-existent doc_id returns error."""
    result = json.loads(await kb_document_outline(str(uuid.uuid4())))
    assert "error" in result


async def test_document_outline_no_headings(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
    sample_chunks,
):
    """Document with no headings returns empty list, not error."""
    doc, _ = sample_doc
    result = json.loads(await kb_document_outline(str(doc.doc_id)))
    assert result["headings"] == []
    assert result["page_count"] == 3
    assert result["chunk_count"] == 5


# ---------------------------------------------------------------------------
# kb_entity_search & kb_entity_overview
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_entities(db_session: AsyncSession, sample_doc, sample_chunks):
    """Create entities for the sample document's chunks."""
    doc, version = sample_doc
    chunks = sample_chunks
    entities = []
    entity_data = [
        (chunks[0], "John Smith", "PERSON", 0, 10),
        (chunks[0], "Acme Corp", "ORG", 15, 24),
        (chunks[1], "John Smith", "PERSON", 5, 15),
        (chunks[1], "New York", "GPE", 20, 28),
        (chunks[2], "Paris", "GPE", 0, 5),
    ]
    for chunk, text, etype, start, end in entity_data:
        e = Entity(
            version_id=version.version_id,
            chunk_id=chunk.chunk_id,
            doc_id=doc.doc_id,
            entity_text=text,
            entity_type=etype,
            start_char=start,
            end_char=end,
        )
        db_session.add(e)
        entities.append(e)
    await db_session.flush()
    return entities


async def test_entity_search_happy(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Search for entities by text substring."""
    result = json.loads(await kb_entity_search("John"))
    assert result["total"] >= 2
    assert any(e["entity_text"] == "John Smith" for e in result["entities"])


async def test_entity_search_by_type(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Filter entities by type."""
    result = json.loads(await kb_entity_search("", entity_type="PERSON"))
    for e in result["entities"]:
        assert e["entity_type"] == "PERSON"


async def test_entity_search_deduplicated(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Deduplicate mode groups by entity_text+entity_type with counts."""
    result = json.loads(await kb_entity_search("John", deduplicate=True))
    assert result["total"] == 1  # one unique "John Smith" PERSON
    assert result["entities"][0]["mention_count"] == 2


async def test_entity_overview_happy(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Entity overview returns type distribution and top entities."""
    result = json.loads(await kb_entity_overview())
    assert result["total_entities"] == 5
    assert result["unique_entities"] == 4  # John Smith, Acme Corp, New York, Paris
    assert "PERSON" in result["type_distribution"]
    assert "GPE" in result["type_distribution"]
    assert len(result["top_entities"]) > 0
    # John Smith has 2 mentions, should be at or near top
    john = next((e for e in result["top_entities"] if e["entity_text"] == "John Smith"), None)
    assert john is not None
    assert john["mention_count"] == 2


async def test_entity_overview_empty(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Empty corpus returns zeros."""
    result = json.loads(await kb_entity_overview())
    assert result["total_entities"] == 0
    assert result["unique_entities"] == 0
    assert result["type_distribution"] == {}
    assert result["top_entities"] == []


# ---------------------------------------------------------------------------
# Entity co-occurrence tests
# ---------------------------------------------------------------------------


async def test_entity_cooccurrence_chunk_scope(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Chunk scope: entities in same chunk as 'John Smith'."""
    result = json.loads(await kb_entity_cooccurrence("John Smith", scope="chunk"))
    assert result["scope"] == "chunk"
    texts = {c["entity_text"] for c in result["cooccurrences"]}
    assert "Acme Corp" in texts
    assert "New York" in texts
    assert "Paris" not in texts  # Paris is in a different chunk
    for c in result["cooccurrences"]:
        assert c["cooccurrence_count"] == 1


async def test_entity_cooccurrence_document_scope(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Document scope: all entities in same version as 'Paris'."""
    result = json.loads(await kb_entity_cooccurrence("Paris", scope="document"))
    texts = {c["entity_text"] for c in result["cooccurrences"]}
    assert "John Smith" in texts
    assert "Acme Corp" in texts
    assert "New York" in texts
    # John Smith appears in 2 chunks in the same version
    john = next(c for c in result["cooccurrences"] if c["entity_text"] == "John Smith")
    assert john["cooccurrence_count"] == 2


async def test_entity_cooccurrence_cooccur_type_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Filter co-occurring entities by type."""
    result = json.loads(await kb_entity_cooccurrence("John Smith", cooccur_type="ORG"))
    assert all(c["entity_type"] == "ORG" for c in result["cooccurrences"])
    texts = {c["entity_text"] for c in result["cooccurrences"]}
    assert "Acme Corp" in texts
    assert "New York" not in texts


async def test_entity_cooccurrence_source_type_filter(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Filter source entity by type."""
    result = json.loads(await kb_entity_cooccurrence("John", entity_type="PERSON", scope="chunk"))
    texts = {c["entity_text"] for c in result["cooccurrences"]}
    assert "Acme Corp" in texts
    assert "New York" in texts


async def test_entity_cooccurrence_no_results_chunk(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Paris is alone in its chunk — no co-occurrences at chunk scope."""
    result = json.loads(await kb_entity_cooccurrence("Paris", scope="chunk"))
    assert result["cooccurrences"] == []
    assert result["total"] == 0


async def test_entity_cooccurrence_invalid_scope(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Invalid scope returns error."""
    result = json.loads(await kb_entity_cooccurrence("John", scope="paragraph"))
    assert "error" in result


async def test_entity_cooccurrence_doc_id_scoped(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Scope to a specific document."""
    doc, _ = sample_doc
    result = json.loads(await kb_entity_cooccurrence("John Smith", doc_id=str(doc.doc_id)))
    texts = {c["entity_text"] for c in result["cooccurrences"]}
    assert "Acme Corp" in texts
    assert "New York" in texts


async def test_entity_cooccurrence_entity_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
    sample_entities,
):
    """Nonexistent entity returns empty results."""
    result = json.loads(await kb_entity_cooccurrence("Nonexistent Entity XYZ"))
    assert result["cooccurrences"] == []
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# kb_read_document
# ---------------------------------------------------------------------------


async def test_read_document_full(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """Full document returns all pages in order."""
    doc, version = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id)))
    assert result["doc_id"] == str(doc.doc_id)
    assert result["version_id"] == str(version.version_id)
    assert result["title"] == "Test Document"
    assert result["page_count"] == 3
    assert result["pages_returned"] == 3
    assert result["total_chars"] > 0
    assert result["truncated"] is False
    assert result["pages"][0]["page_num"] == 1
    assert result["pages"][1]["page_num"] == 2
    assert result["pages"][2]["page_num"] == 3


async def test_read_document_page_range(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """Page range returns only requested pages."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), page_start=1, page_end=2))
    assert result["pages_returned"] == 2
    assert [p["page_num"] for p in result["pages"]] == [1, 2]


async def test_read_document_single_page(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """Single page request returns exactly that page."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), page_start=2, page_end=2))
    assert result["pages_returned"] == 1
    assert result["pages"][0]["page_num"] == 2
    assert result["pages"][0]["text"] == "Page 2 content."


async def test_read_document_max_chars_truncation(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """max_chars truncates output and sets truncated=true."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), max_chars=20))
    assert result["truncated"] is True
    assert result["total_chars"] <= 20


async def test_read_document_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Non-existent doc_id returns error."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    result = json.loads(await kb_read_document(fake_id))
    assert "error" in result
    assert result["error"] == "Document not found"


async def test_read_document_chunk_fallback(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """When no pages exist, falls back to chunks."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id)))
    assert result["source"] == "chunks"
    assert result["page_count"] == 0
    assert result["pages_returned"] == 5
    assert result["pages"][0]["chunk_num"] == 0
    assert result["truncated"] is False


async def test_read_document_chunk_fallback_with_page_range_note(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """Chunk fallback warns when page_start/page_end are provided."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), page_start=1, page_end=2))
    assert result["source"] == "chunks"
    assert "note" in result
    assert "ignored" in result["note"]


async def test_read_document_chunk_fallback_truncation(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_chunks,
):
    """max_chars truncation works in the chunk fallback path."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), max_chars=20))
    assert result["source"] == "chunks"
    assert result["truncated"] is True
    assert result["total_chars"] <= 20


async def test_read_document_invalid_uuid(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
):
    """Malformed doc_id returns error, not an exception."""
    result = json.loads(await kb_read_document("not-a-uuid"))
    assert "error" in result
    assert "Invalid doc_id" in result["error"]


async def test_read_document_inactive_not_found(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
):
    """Archived/inactive document returns not found."""
    doc, _ = sample_doc
    doc.status = "archived"
    await db_session.flush()
    result = json.loads(await kb_read_document(str(doc.doc_id)))
    assert "error" in result
    assert result["error"] == "Document not found"


async def test_read_document_pages_source_field(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """Pages path includes source='pages' for consistent response shape."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id)))
    assert result["source"] == "pages"


async def test_read_document_page_range_beyond_bounds(
    db_session,
    admin_user,
    mcp_principal,
    mock_session_factory,
    sample_doc,
    sample_pages,
):
    """Page range beyond actual pages returns empty."""
    doc, _ = sample_doc
    result = json.loads(await kb_read_document(str(doc.doc_id), page_start=10, page_end=20))
    assert result["pages_returned"] == 0
    assert result["pages"] == []
    assert result["total_chars"] == 0
    assert result["truncated"] is False
