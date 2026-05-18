"""Tests for ScoreBreakdown + reranker_status additions to search schemas."""

from harbor_clerk.api.schemas.search import (
    ScoreBreakdown,
    SearchHitOut,
    SearchResponse,
)


def test_score_breakdown_serializes_with_optional_reranker():
    sb = ScoreBreakdown(fts=0.3, vector=0.5, hybrid=0.4, reranker=0.9)
    assert sb.model_dump() == {"fts": 0.3, "vector": 0.5, "hybrid": 0.4, "reranker": 0.9}

    sb_no_rerank = ScoreBreakdown(fts=0.3, vector=0.5, hybrid=0.4, reranker=None)
    assert sb_no_rerank.model_dump()["reranker"] is None


def test_search_hit_out_score_breakdown_is_optional():
    """Existing clients that don't know about score_breakdown still work."""
    hit = SearchHitOut(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="00000000-0000-0000-0000-000000000001",
        chunk_num=0,
        chunk_text="hello",
        page_start=1,
        page_end=1,
        language="en",
        ocr_used=False,
        ocr_confidence=None,
        score=0.5,
        doc_title="A doc",
        score_breakdown=None,
    )
    assert hit.score_breakdown is None


def test_search_response_has_reranker_status():
    resp = SearchResponse(
        hits=[],
        total_candidates=0,
        has_more=False,
        possible_conflict=False,
        conflict_sources=[],
        reranker_status="disabled",
    )
    assert resp.reranker_status == "disabled"
