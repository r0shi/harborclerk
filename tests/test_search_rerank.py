"""Tests for rerank_hits — calls reranker service, falls back on failure."""

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from harbor_clerk.search import SearchHit
from harbor_clerk.search_rerank import rerank_hits


def _hit(doc_id: str, doc_title: str, chunk_text: str, score: float) -> SearchHit:
    return SearchHit(
        chunk_id=f"chunk-{doc_id}",
        doc_id=doc_id,
        chunk_num=0,
        chunk_text=chunk_text,
        page_start=1,
        page_end=1,
        language="en",
        ocr_used=False,
        ocr_confidence=None,
        score=score,
        doc_title=doc_title,
    )


@pytest.mark.asyncio
async def test_rerank_hits_reorders_by_reranker_score(httpx_mock: HTTPXMock):
    hits = [
        _hit("doc-a", "A", "first chunk", 0.5),
        _hit("doc-b", "B", "second chunk", 0.9),
        _hit("doc-c", "C", "third chunk", 0.3),
    ]
    # Reranker reverses the order: index 2 best, then 0, then 1
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [
                {"index": 2, "score": 0.95},
                {"index": 0, "score": 0.50},
                {"index": 1, "score": 0.10},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    reranked = await rerank_hits("query", hits, top_k=2)
    assert [h.doc_id for h in reranked] == ["doc-c", "doc-a"]
    assert reranked[0].score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_rerank_hits_fallback_on_http_error(httpx_mock: HTTPXMock):
    """Reranker 500 → log warning, return hits[:top_k] unchanged."""
    hits = [
        _hit("doc-a", "A", "x", 0.5),
        _hit("doc-b", "B", "y", 0.3),
    ]
    httpx_mock.add_response(url="http://reranker:8001/rerank", status_code=500)
    result, status = await rerank_hits("q", hits, top_k=2, return_status=True)
    assert [h.doc_id for h in result] == ["doc-a", "doc-b"]
    assert status == "failed"


@pytest.mark.asyncio
async def test_rerank_hits_strict_mode_raises_on_failure(httpx_mock: HTTPXMock, monkeypatch):
    """When settings.reranker_strict=True, HTTP failure raises."""
    from harbor_clerk import search_rerank

    monkeypatch.setattr(
        search_rerank,
        "_settings",
        lambda: type(
            "S",
            (),
            {
                "reranker_url": "http://reranker:8001",
                "reranker_strict": True,
                "reranker_timeout_seconds": 30.0,
                "reranker_pool_size": 50,
            },
        )(),
    )
    httpx_mock.add_response(url="http://reranker:8001/rerank", status_code=500)
    with pytest.raises(httpx.HTTPError):
        await rerank_hits("q", [_hit("a", "A", "x", 0.5)], top_k=1)


@pytest.mark.asyncio
async def test_rerank_hits_empty_input_returns_empty(httpx_mock: HTTPXMock):
    """No hits in → no hits out, no HTTP call."""
    result = await rerank_hits("q", [], top_k=10)
    assert result == []


@pytest.mark.asyncio
async def test_rerank_hits_caps_at_pool_size(httpx_mock: HTTPXMock, monkeypatch):
    """If len(hits) > pool_size, only the first pool_size are sent."""
    from harbor_clerk import search_rerank

    monkeypatch.setattr(
        search_rerank,
        "_settings",
        lambda: type(
            "S",
            (),
            {
                "reranker_url": "http://reranker:8001",
                "reranker_strict": False,
                "reranker_timeout_seconds": 30.0,
                "reranker_pool_size": 3,
            },
        )(),
    )
    hits = [_hit(f"d{i}", f"D{i}", f"x{i}", 0.5) for i in range(10)]
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.8}, {"index": 2, "score": 0.7}],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    result = await rerank_hits("q", hits, top_k=3)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    assert len(body["passages"]) == 3
    assert len(result) == 3


@pytest.mark.asyncio
async def test_rerank_hits_null_score_falls_back_to_hybrid_order(httpx_mock: HTTPXMock):
    """#699: a NaN from the CrossEncoder arrives as JSON null. Installing it as a
    hit score crashed hybrid_search's conflict detection (`None * 0.9`). The
    client must treat the body like an HTTP failure: hybrid order, "failed"."""
    hits = [
        _hit("doc-a", "A", "x", 0.5),
        _hit("doc-b", "B", "y", 0.3),
        _hit("doc-c", "C", "z", 0.1),
    ]
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [
                {"index": 2, "score": 0.9},
                {"index": 0, "score": None},
                {"index": 1, "score": 0.1},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    result, status = await rerank_hits("q", hits, top_k=2, return_status=True)
    assert status == "failed"
    assert [h.doc_id for h in result] == ["doc-a", "doc-b"], "must not partially apply a reranking"
    assert [h.score for h in result] == [0.5, 0.3]
    assert all(h.score is not None for h in result)


@pytest.mark.asyncio
async def test_rerank_hits_out_of_range_index_falls_back_to_hybrid_order(httpx_mock: HTTPXMock):
    hits = [
        _hit("doc-a", "A", "x", 0.5),
        _hit("doc-b", "B", "y", 0.3),
    ]
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [{"index": 1, "score": 0.9}, {"index": 7, "score": 0.8}],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    result, status = await rerank_hits("q", hits, top_k=2, return_status=True)
    assert status == "failed"
    assert [h.doc_id for h in result] == ["doc-a", "doc-b"]


@pytest.mark.asyncio
async def test_rerank_hits_strict_mode_raises_on_null_score(httpx_mock: HTTPXMock, monkeypatch):
    """Strict mode fails loudly on an unusable body, as it does on an HTTP error."""
    from harbor_clerk import search_rerank

    monkeypatch.setattr(
        search_rerank,
        "_settings",
        lambda: type(
            "S",
            (),
            {
                "reranker_url": "http://reranker:8001",
                "reranker_strict": True,
                "reranker_timeout_seconds": 30.0,
                "reranker_pool_size": 50,
            },
        )(),
    )
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={"scores": [{"index": 0, "score": None}], "model": "m"},
    )
    with pytest.raises(search_rerank.RerankResponseError):
        await rerank_hits("q", [_hit("a", "A", "x", 0.5)], top_k=1)
