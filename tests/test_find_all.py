"""Unit tests for find_all() — doc-level enumeration."""

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
    """total_matches reflects ALL matches, even when max_results truncates."""
    await _seed_docs_with_chunks(db_session, n_docs=10, chunks_per_doc=2)

    result = await find_all(db_session, "off-balance-sheet", max_results=3)

    assert len(result.hits) == 3
    assert result.total_matches == 10
    assert result.truncated is True
