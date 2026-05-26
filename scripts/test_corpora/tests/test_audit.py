"""Unit tests for scripts/test_corpora/runner/audit.py.

Fixture-driven: every test constructs the minimum capture/verdict shape
its function needs. No live LLM calls, no DB.
"""

from __future__ import annotations

from scripts.test_corpora.runner.audit import citation_hygiene, failure_correlation, tool_use_stats


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


def _verdict(qid: str, correctness: int = 5, rationale: str = "ok") -> dict:
    """Build a minimum-shape verdict for tests. qid is paired in the joined dict layer."""
    return {
        "id": qid,
        "correctness": correctness,
        "groundedness": 5,
        "completeness": 5,
        "rationale": rationale,
        "source": {},
    }


def test_failure_correlation_low_correctness_low_tool_use():
    """correctness <= 2 AND tool_count <= 1 surfaces the qid."""
    caps = [
        _cap("q-fail", ["kb_search"]),  # 1 tool call
        _cap("q-iterated", ["kb_search"] * 3),  # 3 tool calls
        _cap("q-ok", ["kb_search"]),
    ]
    verdicts = [
        _verdict("q-fail", correctness=1),
        _verdict("q-iterated", correctness=1),
        _verdict("q-ok", correctness=5),
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["low_correctness_low_tool_use"]}
    # q-fail: correctness=1, tool_count=1 -> qualifies
    # q-iterated: correctness=1 but tool_count=3 -> does NOT qualify
    # q-ok: tool_count=1 but correctness=5 -> does NOT qualify
    assert qids == {"q-fail"}


def test_failure_correlation_earliest_latest_questions_no_by_date_tool():
    """qid contains earliest/latest/oldest/newest/first/last AND no
    kb_documents_by_date call surfaces the qid."""
    caps = [
        _cap("enron-earliest-california", ["kb_search"]),
        _cap("synth-latest-contract", ["kb_documents_by_date"]),  # has the right tool
        _cap("enron-find-something", ["kb_search"]),  # name doesn't trigger
    ]
    verdicts = [
        _verdict("enron-earliest-california", correctness=0),
        _verdict("synth-latest-contract", correctness=5),
        _verdict("enron-find-something", correctness=3),
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["earliest_latest_questions_no_by_date_tool"]}
    assert qids == {"enron-earliest-california"}


def test_failure_correlation_ambiguous_id_questions_no_verify_tool():
    """correctness <= 2 AND rationale matches an ambiguity trigger AND no
    kb_verify_identifier call surfaces the qid."""
    caps = [
        _cap("q-ambig", ["kb_search"]),
        _cap("q-verified", ["kb_verify_identifier", "kb_search"]),
        _cap("q-not-flagged", ["kb_search"]),
    ]
    verdicts = [
        _verdict("q-ambig", correctness=1, rationale="answer is ambiguous between multiple docs"),
        _verdict("q-verified", correctness=1, rationale="answer is ambiguous"),  # has verify -> not flagged
        _verdict("q-not-flagged", correctness=1, rationale="answer is wrong"),  # no ambiguity word -> not flagged
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["ambiguous_id_questions_no_verify_tool"]}
    assert qids == {"q-ambig"}


def test_failure_correlation_empty_lists_when_no_matches():
    """Pattern keys MUST be present with [] when nothing matches."""
    caps = [_cap("q1", ["kb_search"] * 3)]
    verdicts = [_verdict("q1", correctness=5)]
    result = failure_correlation(caps, verdicts)
    assert result["low_correctness_low_tool_use"] == []
    assert result["earliest_latest_questions_no_by_date_tool"] == []
    assert result["ambiguous_id_questions_no_verify_tool"] == []


def test_failure_correlation_handles_missing_verdict():
    """Capture without a paired verdict is skipped (not crashed)."""
    caps = [_cap("q-no-verdict", ["kb_search"]), _cap("q-paired", ["kb_search"])]
    verdicts = [_verdict("q-paired", correctness=1)]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["low_correctness_low_tool_use"]}
    assert qids == {"q-paired"}  # the unpaired capture was silently skipped


def _cap_with_transcript_uuids(qid: str, cited: list[str], transcript_uuids: list[str]) -> dict:
    """Build a capture where the tool transcript result_summary mentions the
    given UUIDs as doc_id values (so the UUID extractor will find them)."""
    import json as _json

    transcript = [
        {
            "tool": "kb_search",
            "args": {},
            "result_summary": _json.dumps({"hits": [{"doc_id": u, "score": 0.9} for u in transcript_uuids]}),
        }
    ]
    return {
        "question_id": qid,
        "question": "q",
        "answer": "an answer with some prose",
        "cited_doc_ids": cited,
        "cited_doc_titles": [],
        "tool_call_count": 1,
        "tool_transcript": transcript,
        "elapsed_seconds": 0.0,
        "model": "test",
        "timestamp": "2026-05-25T00:00:00Z",
    }


# Use real UUIDs (regex-shaped); strings differ to make assertions clearer.
_U1 = "11111111-1111-1111-1111-111111111111"
_U2 = "22222222-2222-2222-2222-222222222222"
_U3 = "33333333-3333-3333-3333-333333333333"


def test_citation_hygiene_grounded_when_cited_subset_of_seen():
    caps = [_cap_with_transcript_uuids("q1", cited=[_U1], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 1
    assert result["total"] == 1
    assert result["fabricated_citations"] == []
    assert result["no_citations"] == []


def test_citation_hygiene_fabricated_when_cited_not_seen():
    caps = [_cap_with_transcript_uuids("q1", cited=[_U3], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0
    fab = result["fabricated_citations"]
    assert len(fab) == 1
    assert fab[0]["qid"] == "q1"
    assert _U3 in fab[0]["cited"]
    assert set(fab[0]["seen_in_transcript"]) == {_U1, _U2}


def test_citation_hygiene_no_citations_bucket():
    caps = [_cap_with_transcript_uuids("q1", cited=[], transcript_uuids=[_U1])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0  # no cited == no grounded claim either
    assert len(result["no_citations"]) == 1
    nc = result["no_citations"][0]
    assert nc["qid"] == "q1"
    assert nc["answer_preview"].startswith("an answer")


def test_citation_hygiene_partial_fabrication_counts_as_fabricated():
    """If ANY cited doc_id isn't in the transcript, the capture goes in
    fabricated_citations — not grounded_count."""
    caps = [_cap_with_transcript_uuids("q1", cited=[_U1, _U3], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0
    assert len(result["fabricated_citations"]) == 1


def test_citation_hygiene_buckets_partition_total():
    """grounded_count + len(no_citations) + len(fabricated_citations) == total
    for any set of captures (each capture lives in exactly one bucket)."""
    caps = [
        _cap_with_transcript_uuids("q-grounded", cited=[_U1], transcript_uuids=[_U1]),
        _cap_with_transcript_uuids("q-no-cite", cited=[], transcript_uuids=[_U1]),
        _cap_with_transcript_uuids("q-fab", cited=[_U3], transcript_uuids=[_U1]),
    ]
    result = citation_hygiene(caps)
    assert result["total"] == 3
    assert result["grounded_count"] + len(result["no_citations"]) + len(result["fabricated_citations"]) == 3
