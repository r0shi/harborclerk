"""Tests for harbor_clerk.llm.citations — extractor + dedup."""

from __future__ import annotations

import json

from harbor_clerk.llm.citations import dedupe_citations, extract_citations_from_tool_result

# ── shape coverage ──


def test_extract_from_kb_search_hits() -> None:
    raw = json.dumps(
        {
            "hits": [
                {
                    "chunk_id": "11111111-1111-1111-1111-111111111111",
                    "doc_id": "22222222-2222-2222-2222-222222222222",
                    "doc_title": "Vendor Agreement v3",
                    "score": 0.82,
                    "pages": "3-4",
                },
                {
                    "chunk_id": "33333333-3333-3333-3333-333333333333",
                    "doc_id": "44444444-4444-4444-4444-444444444444",
                    "doc_title": "Master Service Agreement",
                    "score": 0.71,
                },
            ],
            "total_candidates": 12,
            "has_more": True,
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert len(cites) == 2
    assert cites[0]["doc_id"] == "22222222-2222-2222-2222-222222222222"
    assert cites[0]["doc_title"] == "Vendor Agreement v3"
    assert cites[0]["chunk_id"] == "11111111-1111-1111-1111-111111111111"
    assert cites[0]["pages"] == "3-4"
    assert cites[0]["score"] == 0.82
    # Second hit has no pages — must not crash and must not invent the field.
    assert "pages" not in cites[1]


def test_extract_from_kb_batch_search() -> None:
    raw = json.dumps(
        {
            "results": [
                {"query": "X", "hits": [{"doc_id": "aa", "chunk_id": "c1"}]},
                {"query": "Y", "hits": [{"doc_id": "bb", "chunk_id": "c2"}]},
            ]
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert [c["doc_id"] for c in cites] == ["aa", "bb"]
    assert [c["chunk_id"] for c in cites] == ["c1", "c2"]


def test_extract_from_kb_read_passages_keeps_chunk_only() -> None:
    # kb_read_passages does NOT include doc_id — passages carry chunk_id +
    # doc_title only. We still capture them so the UI can render the passage
    # and a chunk→doc join later can resolve doc_id.
    raw = json.dumps(
        {
            "passages": [
                {"chunk_id": "c1", "doc_title": "X", "pages": "5", "text": "..."},
                {"chunk_id": "c2", "doc_title": "Y", "text": "..."},
            ]
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert len(cites) == 2
    assert cites[0]["chunk_id"] == "c1"
    assert cites[0]["doc_title"] == "X"
    assert "doc_id" not in cites[0]


def test_extract_from_kb_expand_context_inherits_doc_id() -> None:
    raw = json.dumps(
        {
            "doc_id": "DOC1",
            "doc_title": "Title A",
            "chunks": [
                {"chunk_id": "c1", "chunk_num": 4, "pages": "2"},
                {"chunk_id": "c2", "chunk_num": 5, "is_target": True},
            ],
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert len(cites) == 2
    assert all(c["doc_id"] == "DOC1" for c in cites)
    assert all(c["doc_title"] == "Title A" for c in cites)


def test_extract_from_kb_list_recent_documents() -> None:
    raw = json.dumps(
        {
            "total_count": 2,
            "documents": [
                {"doc_id": "d1", "title": "Doc 1"},
                {"doc_id": "d2", "title": "Doc 2"},
            ],
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert {c["doc_id"] for c in cites} == {"d1", "d2"}
    assert cites[0]["doc_title"] == "Doc 1"


def test_extract_from_kb_find_related() -> None:
    raw = json.dumps(
        {
            "doc_id": "ROOT",
            "related": [
                {"doc_id": "r1", "title": "Related 1"},
                {"doc_id": "r2", "title": "Related 2"},
            ],
        }
    )
    cites = extract_citations_from_tool_result(raw)
    # The root doc plus two related docs would over-count what the user is
    # citing; ``related`` callers are using it to follow leads, so only the
    # related list is treated as citations.
    assert [c["doc_id"] for c in cites] == ["r1", "r2"]


def test_extract_from_kb_get_document() -> None:
    raw = json.dumps(
        {
            "doc_id": "D1",
            "title": "Single Doc",
            "mime_type": "application/pdf",
            "size_bytes": 12345,
        }
    )
    cites = extract_citations_from_tool_result(raw)
    assert cites == [{"doc_id": "D1", "doc_title": "Single Doc"}]


# ── error/edge cases ──


def test_error_payload_returns_empty() -> None:
    assert extract_citations_from_tool_result(json.dumps({"error": "oh no"})) == []


def test_non_json_returns_empty() -> None:
    assert extract_citations_from_tool_result("not json at all") == []


def test_non_object_json_returns_empty() -> None:
    assert extract_citations_from_tool_result("[]") == []
    assert extract_citations_from_tool_result("null") == []
    assert extract_citations_from_tool_result('"hello"') == []


def test_empty_hits_returns_empty() -> None:
    assert extract_citations_from_tool_result(json.dumps({"hits": []})) == []


def test_unrelated_tool_shape_returns_empty() -> None:
    # kb_entity_overview, kb_system_health, etc. — no citation shape.
    assert extract_citations_from_tool_result(json.dumps({"total_entities": 42})) == []
    assert extract_citations_from_tool_result(json.dumps({"status": "healthy"})) == []


def test_hit_without_doc_id_or_chunk_id_skipped() -> None:
    raw = json.dumps({"hits": [{"score": 0.9}, {"doc_id": "d1"}]})
    cites = extract_citations_from_tool_result(raw)
    assert len(cites) == 1
    assert cites[0]["doc_id"] == "d1"


def test_score_must_be_numeric() -> None:
    raw = json.dumps({"hits": [{"doc_id": "d1", "score": "high"}]})
    cites = extract_citations_from_tool_result(raw)
    assert "score" not in cites[0]


# ── dedup ──


def test_dedupe_keeps_first_order() -> None:
    cites = [
        {"doc_id": "a", "chunk_id": "x"},
        {"doc_id": "b", "chunk_id": "y"},
        {"doc_id": "a", "chunk_id": "x"},
    ]
    out = dedupe_citations(cites)
    assert [(c["doc_id"], c["chunk_id"]) for c in out] == [("a", "x"), ("b", "y")]


def test_dedupe_keeps_highest_score() -> None:
    cites = [
        {"doc_id": "a", "chunk_id": "x", "score": 0.5},
        {"doc_id": "a", "chunk_id": "x", "score": 0.9},
        {"doc_id": "a", "chunk_id": "x", "score": 0.3},
    ]
    out = dedupe_citations(cites)
    assert len(out) == 1
    assert out[0]["score"] == 0.9


def test_dedupe_doc_only_vs_chunk() -> None:
    # Same doc, different chunks → kept separate. Same doc, no chunk → its
    # own dedup bucket.
    cites = [
        {"doc_id": "a", "chunk_id": "x"},
        {"doc_id": "a", "chunk_id": "y"},
        {"doc_id": "a"},
        {"doc_id": "a"},
    ]
    out = dedupe_citations(cites)
    assert len(out) == 3


def test_dedupe_drops_empty_records() -> None:
    cites: list[dict] = [{"doc_id": "a"}, {}, "not a dict"]  # type: ignore[list-item]
    out = dedupe_citations(cites)
    assert out == [{"doc_id": "a"}]


def test_dedupe_chunk_only_records_kept() -> None:
    # kb_read_passages produces chunk-only records — dedupe must keep them
    # rather than dropping for missing doc_id.
    cites = [
        {"chunk_id": "c1", "doc_title": "X"},
        {"chunk_id": "c1", "doc_title": "X"},
    ]
    out = dedupe_citations(cites)
    assert len(out) == 1
    assert out[0]["chunk_id"] == "c1"
