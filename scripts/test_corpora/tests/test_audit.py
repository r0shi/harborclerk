"""Unit tests for scripts/test_corpora/runner/audit.py.

Fixture-driven: every test constructs the minimum capture/verdict shape
its function needs. No live LLM calls, no DB.
"""

from __future__ import annotations

from scripts.test_corpora.runner.audit import tool_use_stats


def _cap(qid: str, tools: list[str], **overrides) -> dict:
    """Build a minimum-shape capture for tests."""
    base = {
        "question_id": qid,
        "question": "q",
        "answer": "a",
        "cited_doc_ids": [],
        "cited_doc_titles": [],
        "tool_call_count": len(tools),
        "tool_transcript": [{"tool": t, "args": {}, "result_summary": "{}"} for t in tools],
        "elapsed_seconds": 0.0,
        "model": "test-model",
        "timestamp": "2026-05-25T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_tool_use_stats_empty_captures():
    result = tool_use_stats([])
    assert result == {
        "total_captures": 0,
        "tool_call_distribution": {},
        "tool_call_counts_per_tool": {},
        "captures_by_tool_count": {},
    }


def test_tool_use_stats_single_capture_single_tool():
    caps = [_cap("q1", ["kb_search"])]
    result = tool_use_stats(caps)
    assert result["total_captures"] == 1
    assert result["tool_call_distribution"] == {1: 1}
    assert result["tool_call_counts_per_tool"] == {"kb_search": 1}
    assert result["captures_by_tool_count"]["q1"] == {
        "tool_count": 1,
        "tools_used": ["kb_search"],
    }


def test_tool_use_stats_mixed_captures_distribution():
    caps = [
        _cap("q0", []),
        _cap("q1", ["kb_search"]),
        _cap("q1b", ["kb_search"]),
        _cap("q2", ["kb_search", "kb_get_document"]),
        _cap("q4plus", ["kb_search"] * 5),
    ]
    result = tool_use_stats(caps)
    assert result["total_captures"] == 5
    # 0 -> 1, 1 -> 2, 2 -> 1, "4+" -> 1
    assert result["tool_call_distribution"] == {0: 1, 1: 2, 2: 1, "4+": 1}


def test_tool_use_stats_per_tool_counts():
    caps = [
        _cap("q1", ["kb_search", "kb_search", "kb_get_document"]),
        _cap("q2", ["kb_search"]),
    ]
    result = tool_use_stats(caps)
    assert result["tool_call_counts_per_tool"] == {
        "kb_search": 3,
        "kb_get_document": 1,
    }


def test_tool_use_stats_captures_by_tool_count_records_per_qid():
    caps = [_cap("q1", ["kb_search", "kb_get_document"])]
    result = tool_use_stats(caps)
    assert result["captures_by_tool_count"]["q1"]["tool_count"] == 2
    assert result["captures_by_tool_count"]["q1"]["tools_used"] == ["kb_search", "kb_get_document"]


def test_tool_use_stats_4_plus_bucket_includes_exactly_4_and_more():
    """The "4+" bin must catch tool_count == 4 AND tool_count >= 5."""
    caps = [
        _cap("q4", ["kb_search"] * 4),
        _cap("q5", ["kb_search"] * 5),
        _cap("q10", ["kb_search"] * 10),
    ]
    result = tool_use_stats(caps)
    assert result["tool_call_distribution"] == {"4+": 3}
