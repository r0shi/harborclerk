"""Integration tests for hybrid_search() with filtering parameters."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Chunk, Document, DocumentVersion
from harbor_clerk.models.enums import VersionStatus
from harbor_clerk.search import hybrid_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_docs(db_session: AsyncSession):
    """Create 2 documents with different languages, MIME types, and dates.

    doc_a: PDF, english chunks, created "2025-01-15"
    doc_b: text/plain, french chunks, created "2025-06-15"
    """
    # --- Document A ---
    doc_a = Document(title="Report Alpha", status="active")
    db_session.add(doc_a)
    await db_session.flush()

    ver_a = DocumentVersion(
        doc_id=doc_a.doc_id,
        original_sha256=b"sha256_alpha_aaaaaaaaaaaaaaaa",
        original_bucket="originals",
        original_object_key=f"originals/versions/{uuid.uuid4()}/alpha.pdf",
        mime_type="application/pdf",
        size_bytes=10000,
        status=VersionStatus.ready,
        source_path="alpha.pdf",
    )
    db_session.add(ver_a)
    await db_session.flush()
    doc_a.latest_version_id = ver_a.version_id
    # Backdate created_at
    ver_a.created_at = datetime(2025, 1, 15, tzinfo=timezone.utc)
    await db_session.flush()

    for i in range(3):
        db_session.add(
            Chunk(
                version_id=ver_a.version_id,
                doc_id=doc_a.doc_id,
                chunk_num=i,
                page_start=i + 1,
                page_end=i + 1,
                char_start=i * 500,
                char_end=(i + 1) * 500,
                chunk_text=f"Alpha report section {i} about quarterly results.",
                language="english",
            )
        )
    await db_session.flush()

    # --- Document B ---
    doc_b = Document(title="Rapport Beta", status="active")
    db_session.add(doc_b)
    await db_session.flush()

    ver_b = DocumentVersion(
        doc_id=doc_b.doc_id,
        original_sha256=b"sha256_beta_bbbbbbbbbbbbbbbbb",
        original_bucket="originals",
        original_object_key=f"originals/versions/{uuid.uuid4()}/beta.txt",
        mime_type="text/plain",
        size_bytes=5000,
        status=VersionStatus.ready,
        source_path="beta.txt",
    )
    db_session.add(ver_b)
    await db_session.flush()
    doc_b.latest_version_id = ver_b.version_id
    ver_b.created_at = datetime(2025, 6, 15, tzinfo=timezone.utc)
    await db_session.flush()

    for i in range(3):
        db_session.add(
            Chunk(
                version_id=ver_b.version_id,
                doc_id=doc_b.doc_id,
                chunk_num=i,
                page_start=i + 1,
                page_end=i + 1,
                char_start=i * 500,
                char_end=(i + 1) * 500,
                chunk_text=f"Beta rapport section {i} sur les résultats trimestriels.",
                language="french",
            )
        )
    await db_session.flush()

    return doc_a, ver_a, doc_b, ver_b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_hybrid_search_multi_doc_filter(db_session, two_docs):
    """doc_ids filter returns only chunks from specified documents."""
    doc_a, ver_a, doc_b, ver_b = two_docs
    result = await hybrid_search(
        db_session,
        "section results",
        k=10,
        doc_ids=[doc_a.doc_id],
    )
    for h in result.hits:
        assert h.doc_id == str(doc_a.doc_id)


async def test_hybrid_search_language_filter(db_session, two_docs):
    """Language filter returns only matching-language chunks."""
    doc_a, ver_a, doc_b, ver_b = two_docs
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        language="french",
    )
    for h in result.hits:
        assert h.language == "french"
        assert h.doc_id == str(doc_b.doc_id)


async def test_hybrid_search_date_range_filter(db_session, two_docs):
    """after/before filters restrict by version created_at."""
    doc_a, ver_a, doc_b, ver_b = two_docs

    # Only versions after 2025-03-01 → should get doc_b only
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        after=datetime(2025, 3, 1, tzinfo=timezone.utc),
    )
    for h in result.hits:
        assert h.doc_id == str(doc_b.doc_id)

    # Only versions before 2025-03-01 → should get doc_a only
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        before=datetime(2025, 3, 1, tzinfo=timezone.utc),
    )
    for h in result.hits:
        assert h.doc_id == str(doc_a.doc_id)


async def test_hybrid_search_mime_type_filter(db_session, two_docs):
    """mime_type filter returns only chunks from matching versions."""
    doc_a, ver_a, doc_b, ver_b = two_docs
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        mime_type="text/plain",
    )
    for h in result.hits:
        assert h.doc_id == str(doc_b.doc_id)


async def test_hybrid_search_combined_filters(db_session, two_docs):
    """Multiple filters combine (AND) correctly."""
    doc_a, ver_a, doc_b, ver_b = two_docs

    # language=french AND mime_type=application/pdf → no results
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        language="french",
        mime_type="application/pdf",
    )
    assert len(result.hits) == 0


async def test_hybrid_search_doc_id_doc_ids_raises(db_session, two_docs):
    """Providing both doc_id and doc_ids raises ValueError."""
    doc_a, ver_a, doc_b, ver_b = two_docs
    with pytest.raises(ValueError, match="both doc_id and doc_ids"):
        await hybrid_search(
            db_session,
            "section",
            k=10,
            doc_id=doc_a.doc_id,
            doc_ids=[doc_b.doc_id],
        )
