"""End-to-end test: hybrid_search calls rerank_hits when settings.reranker_enabled.

The reranker is only invoked when the hybrid stage produced candidates, so
each test seeds one FTS-matchable chunk — an empty corpus would never reach
the rerank branch regardless of the setting.
"""

from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import SearchHit, hybrid_search


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


@pytest.mark.asyncio
async def test_hybrid_search_conflict_detection_survives_a_none_top_score(db_session, monkeypatch):
    """#699: the reranker once handed back a hit whose score was None (a NaN
    serialised as JSON null), and conflict detection crashed on
    `top_score * 0.9`. rerank_hits now refuses such a body, but the crash must
    stay impossible even if a future client change lets a None through: skip
    detection, keep the hits."""
    await _seed_one_chunk(db_session)
    monkeypatch.setattr("harbor_clerk.search.get_settings", lambda: _fake_settings())

    def _hit(doc_id: str, score):
        return SearchHit(
            chunk_id=f"chunk-{doc_id}",
            doc_id=doc_id,
            chunk_num=0,
            chunk_text="x",
            page_start=None,
            page_end=None,
            language="en",
            ocr_used=False,
            ocr_confidence=None,
            score=score,
            doc_title="T",
        )

    leaked = [_hit("11111111-1111-1111-1111-111111111111", None), _hit("22222222-2222-2222-2222-222222222222", 0.4)]
    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock(return_value=(leaked, "ok"))):
        result = await hybrid_search(db_session, query="revenue", k=10)

    assert [h.doc_id for h in result.hits] == [h.doc_id for h in leaked]
    assert result.possible_conflict is False
    assert result.conflict_sources == []

    # A None below the top: the top guard passes, and `float >= None` would
    # raise inside close_hits. The None hit is left out of the comparison; the
    # two finite hits from different docs still count as a conflict.
    leaked = [
        _hit("11111111-1111-1111-1111-111111111111", 0.5),
        _hit("22222222-2222-2222-2222-222222222222", None),
        _hit("33333333-3333-3333-3333-333333333333", 0.48),
    ]
    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock(return_value=(leaked, "ok"))):
        result = await hybrid_search(db_session, query="revenue", k=10)

    assert [h.doc_id for h in result.hits] == [h.doc_id for h in leaked]
    assert result.possible_conflict is True
    assert {c.doc_id for c in result.conflict_sources} == {leaked[0].doc_id, leaked[2].doc_id}
