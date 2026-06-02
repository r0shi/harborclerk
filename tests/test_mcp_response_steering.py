"""Response-level steering for the cloud-LLM MCP surface.

The kb_search/kb_batch_search tool docstrings already carry pagination and
anti-hedge guidance, but the PR-J before/after showed frontier models
reliably ignore tool-description guidance. These tests cover the
response-payload levers that land in context at decision time:

  - `_format_search_response` adds a `continuation` block when `has_more`,
    with an explicit `next_offset` and a directive to drain the result set
    before concluding (targets the Enron find-iteration short-stop).
  - `kb_verify_identifier` adds an anti-hedge `instruction` to the
    not_found response (targets negative-question padding).
  - `_search_stats` tracks `continuation_offered` so the drain rate is
    measurable.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from harbor_clerk.mcp_server import _format_search_response, kb_verify_identifier
from harbor_clerk.search_types import SearchHit, SearchResult


def _hit(i: int) -> SearchHit:
    return SearchHit(
        chunk_id=f"chunk-{i}",
        doc_id=f"doc-{i}",
        chunk_num=i,
        chunk_text=f"text {i}",
        page_start=1,
        page_end=1,
        language="english",
        ocr_used=False,
        ocr_confidence=None,
        score=0.9 - i * 0.01,
        doc_title=f"Doc {i}",
    )


# ---------------------------------------------------------------------------
# Continuation block — active pagination nudge in the response payload
# ---------------------------------------------------------------------------


def test_continuation_block_present_when_has_more():
    # 5 hits returned, k=5, offset=0, but 30 total → more pages exist.
    result = SearchResult(hits=[_hit(i) for i in range(5)], total_candidates=30)
    resp = _format_search_response(result, detail="full", effective_brief_chars=200, k=5, offset=0)

    assert resp["has_more"] is True
    assert "continuation" in resp
    cont = resp["continuation"]
    assert cont["seen"] == 5
    assert cont["total"] == 30
    assert cont["next_offset"] == 5
    # The guidance must name the concrete next_offset and tell the model to
    # paginate before concluding.
    assert "offset=5" in cont["guidance"]
    assert "BEFORE concluding" in cont["guidance"]


def test_continuation_next_offset_advances_with_offset():
    # Second page: offset=5, k=5, still 30 total.
    result = SearchResult(hits=[_hit(i) for i in range(5)], total_candidates=30)
    resp = _format_search_response(result, detail="full", effective_brief_chars=200, k=5, offset=5)

    assert resp["has_more"] is True
    assert resp["continuation"]["next_offset"] == 10
    assert resp["continuation"]["seen"] == 10  # offset + len(hits)


def test_no_continuation_block_when_all_results_fit():
    # 5 hits, k=10, only 5 total → nothing more to drain.
    result = SearchResult(hits=[_hit(i) for i in range(5)], total_candidates=5)
    resp = _format_search_response(result, detail="full", effective_brief_chars=200, k=10, offset=0)

    assert resp["has_more"] is False
    assert "continuation" not in resp


def test_no_continuation_block_on_exact_last_page():
    # offset=25, k=5, total=30 → offset + k == total, no more.
    result = SearchResult(hits=[_hit(i) for i in range(5)], total_candidates=30)
    resp = _format_search_response(result, detail="full", effective_brief_chars=200, k=5, offset=25)

    assert resp["has_more"] is False
    assert "continuation" not in resp


# ---------------------------------------------------------------------------
# kb_verify_identifier — anti-hedge instruction on not_found
# ---------------------------------------------------------------------------


async def test_verify_identifier_not_found_carries_anti_hedge_instruction():
    fake = {"status": "not_found", "identifier": "does-not-exist"}
    with patch("harbor_clerk.mcp_server._verify_identifier_impl", new=AsyncMock(return_value=fake)):
        raw = await kb_verify_identifier("does-not-exist")
    out = json.loads(raw)
    assert out["status"] == "not_found"
    assert "instruction" in out
    # The directive must explicitly forbid the documented padding patterns.
    instr = out["instruction"].lower()
    assert "does not exist" in instr
    assert "closest match" in instr
    assert "do not" in instr


async def test_verify_identifier_unique_has_no_instruction():
    fake = {"status": "unique", "match": {"doc_id": "d1", "title": "T"}}
    with patch("harbor_clerk.mcp_server._verify_identifier_impl", new=AsyncMock(return_value=fake)):
        raw = await kb_verify_identifier("real-doc")
    out = json.loads(raw)
    assert out["status"] == "unique"
    # The anti-hedge directive only belongs on the not_found path.
    assert "instruction" not in out


async def test_verify_identifier_ambiguous_has_no_instruction():
    fake = {"status": "ambiguous", "candidates": [{"doc_id": "a"}, {"doc_id": "b"}]}
    with patch("harbor_clerk.mcp_server._verify_identifier_impl", new=AsyncMock(return_value=fake)):
        raw = await kb_verify_identifier("ambiguous-substring")
    out = json.loads(raw)
    assert out["status"] == "ambiguous"
    assert "instruction" not in out


async def test_verify_identifier_error_shape_gets_no_instruction():
    """`verify_identifier` returns {"error": ...} for empty/whitespace input.
    That shape has no `status` key — the guard must skip it, never attaching
    the anti-hedge directive to an error response."""
    fake = {"error": "identifier must be non-empty"}
    with patch("harbor_clerk.mcp_server._verify_identifier_impl", new=AsyncMock(return_value=fake)):
        raw = await kb_verify_identifier("   ")
    out = json.loads(raw)
    assert "error" in out
    assert "instruction" not in out


# ---------------------------------------------------------------------------
# Stats — continuation_offered counter exists for drain-rate measurement
# ---------------------------------------------------------------------------


def test_search_stats_has_continuation_offered_counter():
    from harbor_clerk.mcp_server import _search_stats

    assert "continuation_offered" in _search_stats
