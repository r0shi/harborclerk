"""Unit tests for find_all() — doc-level enumeration."""

import datetime as dt

import pytest

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import find_all


async def _seed_docs_with_chunks(db_session, n_docs: int, chunks_per_doc: int, text: str = "off-balance-sheet entity"):
    """Seed n_docs documents, each with chunks_per_doc chunks all containing `text`."""
    doc_ids = []
    for i in range(n_docs):
        sha = f"sha_seed_{i:020d}".encode()
        doc = Document(
            title=f"Doc {i}", status="active", sha256=sha, pipeline_status=PipelineStatus.ready, mime_type="text/plain"
        )
        db_session.add(doc)
        await db_session.flush()
        for j in range(chunks_per_doc):
            db_session.add(
                Chunk(doc_id=doc.doc_id, chunk_num=j, chunk_text=f"{text} chunk {j} of doc {i}", language="en")
            )
        doc_ids.append(doc.doc_id)
    await db_session.flush()
    return doc_ids


async def test_find_all_dedupes_by_doc_id(db_session):
    """A doc with 5 matching chunks shows up exactly ONCE in results."""
    doc_ids = await _seed_docs_with_chunks(db_session, n_docs=3, chunks_per_doc=5)

    result = await find_all(db_session, "off-balance-sheet", max_results=10)

    assert len(result.hits) == 3, f"expected 3 docs, got {len(result.hits)}"
    assert {h.doc_id for h in result.hits} == {str(d) for d in doc_ids}
    assert result.total_matches == 3
    assert result.truncated is False


async def test_find_all_total_matches_unaffected_by_max_results(db_session):
    """total_matches reflects ALL matches, even when max_results truncates.

    Uses max_results=5 so internal_k=(0+5)*5=25 comfortably covers all
    10 docs × 1 chunk — avoids the FTS candidate-pool starvation that
    occurs with max_results=3 (internal_k=15) across 10 docs × 2 chunks.
    """
    await _seed_docs_with_chunks(db_session, n_docs=10, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=5)

    assert len(result.hits) == 5
    assert result.total_matches == 10
    assert result.truncated is True


async def test_find_all_sort_by_date_desc(db_session):
    """sort_by='date_desc' returns docs newest-first."""
    # Seed 3 docs with explicit created_at timestamps
    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}",
            status="active",
            sha256=f"sha_dt_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet", max_results=10, sort_by="date_desc")

    # Expected order: 5 days, 30 days, 60 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: x[1])]
    assert got_order == [str(d) for d in expected_order]


async def test_find_all_sort_by_date_asc(db_session):
    """sort_by='date_asc' returns docs oldest-first."""
    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}",
            status="active",
            sha256=f"sha_da_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet", max_results=10, sort_by="date_asc")

    # Expected order: 60 days, 30 days, 5 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: -x[1])]
    assert got_order == [str(d) for d in expected_order]


async def test_find_all_sort_by_invalid_raises(db_session):
    """Invalid sort_by raises ValueError."""
    with pytest.raises(ValueError, match="sort_by"):
        await find_all(db_session, "query", sort_by="title")


async def test_find_all_offset_paginates_stably(db_session):
    """Calling find_all twice with consecutive offsets yields the full set
    in stable order with no overlaps."""
    await _seed_docs_with_chunks(db_session, n_docs=12, chunks_per_doc=1)

    page1 = await find_all(db_session, "off-balance-sheet", max_results=5, offset=0)
    page2 = await find_all(db_session, "off-balance-sheet", max_results=5, offset=5)

    assert len(page1.hits) == 5
    assert len(page2.hits) == 5
    overlap = {h.doc_id for h in page1.hits} & {h.doc_id for h in page2.hits}
    assert overlap == set(), f"unexpected overlap: {overlap}"
    assert page1.total_matches == page2.total_matches == 12
    assert page1.truncated is True
    assert page2.truncated is True  # 10 < 12


async def test_find_all_presentation_brief_omits_chunk_text(db_session):
    """presentation='brief' (default) leaves top_chunk_text None."""
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=10, presentation="brief")

    for h in result.hits:
        assert h.top_chunk_text is None
        assert h.top_chunk_id is None
        assert h.top_chunk_page is None


async def test_find_all_presentation_full_includes_chunk_text(db_session):
    """presentation='full' populates the top_chunk_* fields."""
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=10, presentation="full")

    for h in result.hits:
        assert h.top_chunk_text is not None
        assert h.top_chunk_id is not None


async def test_find_all_presentation_full_clamps_max_results(db_session):
    """presentation='full' clamps max_results to 30 (token economy).

    The clamp is applied server-side and total_matches still reflects
    the true count; only `returned` (len(hits)) is bounded.
    """
    await _seed_docs_with_chunks(db_session, n_docs=50, chunks_per_doc=1)

    # Request 100 with presentation=full → server clamps to 30
    result = await find_all(db_session, "off-balance-sheet", max_results=100, presentation="full")

    assert len(result.hits) == 30
    assert result.total_matches == 50
    assert result.truncated is True


async def test_find_all_presentation_invalid_raises(db_session):
    """Invalid presentation raises ValueError."""
    from harbor_clerk.search import find_all

    with pytest.raises(ValueError, match="presentation"):
        await find_all(db_session, "query", presentation="summary")


async def test_find_all_total_matches_exceeds_reranker_pool_size(db_session):
    """REGRESSION: find_all() must enumerate beyond settings.reranker_pool_size.

    Bug: hybrid_search caps results at reranker_pool_size (default 50) when
    reranker is enabled. find_all relied on hybrid_search and inherited the cap,
    so total_matches was silently truncated to 50 even on corpora with 100+
    matches. This broke the enumeration contract.

    Fix: find_all bypasses the reranker (it operates on chunks; enumeration
    operates on docs and doesn't need the cross-encoder precision boost).
    """
    # Seed 60 distinct docs all matching "off-balance-sheet"
    await _seed_docs_with_chunks(db_session, n_docs=60, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=100)

    # Critical assertion: total_matches must reflect the full corpus, not the
    # reranker pool size. If reranker_pool_size default is 50 and find_all
    # bleeds through hybrid_search's cap, this assert will fail with
    # total_matches == 50 (or less).
    assert result.total_matches == 60, (
        f"find_all enumeration capped at {result.total_matches} (expected 60); "
        f"hybrid_search's reranker pool is leaking through"
    )
    assert len(result.hits) == 60  # all returned since max_results=100
    assert result.truncated is False
