"""End-to-end test: hybrid_search calls rerank_hits when settings.reranker_enabled.

The reranker is only invoked when the hybrid stage produced candidates, so
each test seeds one FTS-matchable chunk — an empty corpus would never reach
the rerank branch regardless of the setting.
"""

from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search


async def _seed_one_chunk(db_session) -> None:
    """Seed a doc + chunk whose text is FTS-matchable on the word 'revenue'."""
    doc = Document(
        title="Reranker Test Doc",
        status="active",
        sha256=b"rerank_test_sha_0000000000000000",
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        Chunk(
            doc_id=doc.doc_id,
            chunk_num=0,
            chunk_text="quarterly revenue figures and termination clauses",
            language="en",
        )
    )
    await db_session.flush()


def _fake_settings(**overrides):
    """Settings stub covering every field hybrid_search + _embed_query touch."""
    base = {
        "reranker_enabled": True,
        "reranker_url": "http://x:1",
        "reranker_strict": False,
        "reranker_timeout_seconds": 30.0,
        "reranker_pool_size": 50,
        "reranker_top_k_pad": 40,
        "embedder_url": "http://embedder:8000",
    }
    base.update(overrides)
    return type("S", (), base)()


@pytest.mark.asyncio
async def test_hybrid_search_calls_reranker_when_enabled(db_session, monkeypatch):
    """When reranker_enabled and candidates exist, hybrid_search invokes
    rerank_hits with top_k = offset + k and return_status=True."""
    await _seed_one_chunk(db_session)
    monkeypatch.setattr("harbor_clerk.search.get_settings", lambda: _fake_settings())

    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock(return_value=([], "ok"))) as mock:
        await hybrid_search(db_session, query="revenue", k=10)
        mock.assert_awaited_once()
        kwargs = mock.await_args.kwargs
        assert kwargs.get("top_k") == 10
        assert kwargs.get("return_status") is True


@pytest.mark.asyncio
async def test_hybrid_search_skips_reranker_when_disabled(db_session, monkeypatch):
    """When reranker_enabled is False, rerank_hits is never called even though
    the corpus has a matching candidate."""
    await _seed_one_chunk(db_session)
    monkeypatch.setattr(
        "harbor_clerk.search.get_settings",
        lambda: _fake_settings(reranker_enabled=False),
    )

    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock()) as mock:
        await hybrid_search(db_session, query="revenue", k=10)
        mock.assert_not_called()
