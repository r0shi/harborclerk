"""Unit tests for research engine internals (harbor_clerk.llm.research)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.llm.research import _extract_notes_with_retry, _is_no_findings_sentinel, _plan_queries, _read_evidence

_DEPTH = {"max_queries": 15, "k_per_query": 20, "max_passages": 60, "gap_round": True, "paginate": True}


@pytest.mark.asyncio
async def test_plan_queries_returns_only_llm_queries_no_seeded_garbage():
    """After dropping corpus seeding, _plan_queries returns exactly the
    LLM-planned queries. The old code concatenated corpus entities with the
    question into ungrammatical seeded queries ('Globex What major vendor
    relationships'); none of those may appear."""
    llm_response = json.dumps({"queries": ["governing law clauses", "termination notice periods"]})
    # entity_overview is what the deleted seeding path called; patching it
    # proves the new code never seeds even when entities are available.
    entity_response = json.dumps({"top_entities": [{"entity_type": "ORG", "entity_text": "Globex"}]})
    with (
        patch("harbor_clerk.llm.research._llm_complete", new=AsyncMock(return_value=llm_response)),
        patch("harbor_clerk.llm.research.execute_tool", new=AsyncMock(return_value=entity_response)),
    ):
        queries = await _plan_queries(
            client=None,
            url="http://llm.test",
            user_question="What are the major vendor relationships?",
            topic_hint=None,
            depth_config=_DEPTH,
            doc_list=None,
            user_id=None,
        )
    assert queries == ["governing law clauses", "termination notice periods"]


@pytest.mark.asyncio
async def test_plan_queries_keyword_fallback_when_llm_yields_nothing():
    """When the LLM returns no plausible queries, _plan_queries falls back to
    the question itself plus two keyword-split halves."""
    with patch("harbor_clerk.llm.research._llm_complete", new=AsyncMock(return_value='{"queries": []}')):
        queries = await _plan_queries(
            client=None,
            url="http://llm.test",
            user_question="What are the major vendor relationships over time",
            topic_hint=None,
            depth_config=_DEPTH,
            doc_list=None,
            user_id=None,
        )
    assert queries[0] == "What are the major vendor relationships over time"
    assert "What are the major" in queries
    assert "vendor relationships over time" in queries


def test_is_no_findings_sentinel_matches_the_sentinel():
    assert _is_no_findings_sentinel("No relevant findings in this passage set.")
    # Tolerant of trailing whitespace / case / wrapping the engine may add.
    assert _is_no_findings_sentinel("  no relevant findings in this passage set  ")
    assert _is_no_findings_sentinel("No relevant findings in this passage set")
    # Weak models often prepend a markdown bullet to the sentinel line.
    assert _is_no_findings_sentinel("- No relevant findings in this passage set.")
    assert _is_no_findings_sentinel("* no relevant findings in this passage set")


def test_is_no_findings_sentinel_rejects_real_notes_and_empty_corpus_string():
    # A real note must not be treated as the bail sentinel.
    assert not _is_no_findings_sentinel("- Acme signed a 30-day termination clause [Doc, page 2]")
    # _extract_notes's genuinely-empty return is a DIFFERENT string and must
    # not match — that case is truly empty and should not trigger a retry.
    assert not _is_no_findings_sentinel("No relevant passages were found in the corpus.")
    assert not _is_no_findings_sentinel("")


_SENTINEL = "No relevant findings in this passage set."
_PASSAGES = "\n---\n**[0265_quarterly_report, page 1]**\nQ1 consulting revenue up 14%.\n"


def _coverage(top_score: float) -> dict:
    return {"c1": {"doc_id": "d1", "doc_title": "0265_quarterly_report", "page": 1, "score": top_score}}


@pytest.mark.asyncio
async def test_retry_not_triggered_when_extraction_succeeds():
    """A normal (non-sentinel) extraction result passes straight through."""
    real_notes = "- Q1 consulting revenue up 14% [0265_quarterly_report, page 1]"
    with patch("harbor_clerk.llm.research._extract_notes", new=AsyncMock(return_value=real_notes)) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out == real_notes
    assert m.await_count == 1  # no retry


@pytest.mark.asyncio
async def test_retry_recovers_when_forceful_extraction_succeeds():
    """Sentinel + high retrieval score → forceful retry; if the retry yields
    real notes, those are used."""
    real_notes = "- Q1 consulting revenue up 14% [0265_quarterly_report, page 1]"
    with patch(
        "harbor_clerk.llm.research._extract_notes",
        new=AsyncMock(side_effect=[_SENTINEL, real_notes]),
    ) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out == real_notes
    assert m.await_count == 2
    assert m.await_args_list[1].kwargs.get("forceful") is True


@pytest.mark.asyncio
async def test_retry_falls_back_to_raw_passages_when_retry_also_bails():
    """Sentinel twice → raw passages become the notes."""
    with patch(
        "harbor_clerk.llm.research._extract_notes",
        new=AsyncMock(side_effect=[_SENTINEL, _SENTINEL]),
    ):
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out.startswith("## Raw passages")
    assert "0265_quarterly_report" in out


@pytest.mark.asyncio
async def test_low_score_sentinel_is_trusted_no_retry():
    """Sentinel + low retrieval score → the sentinel is kept; the corpus
    genuinely lacks the information. No retry."""
    with patch("harbor_clerk.llm.research._extract_notes", new=AsyncMock(return_value=_SENTINEL)) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(0.3))
    assert _is_no_findings_sentinel(out)
    assert m.await_count == 1  # no retry below the relevance floor


@pytest.mark.asyncio
async def test_read_evidence_returns_passages_and_evidence_docs():
    """_read_evidence returns (passages_text, evidence_docs); evidence_docs
    lists the distinct docs whose passages were read into passages_text."""
    coverage = {
        "c1": {"doc_id": "d1", "doc_title": "alpha", "page": 1, "score": 2.0, "snippet": "alpha text"},
        "c2": {"doc_id": "d2", "doc_title": "beta", "page": 3, "score": 1.5, "snippet": "beta text"},
    }
    read_result = json.dumps(
        {
            "passages": [
                {"chunk_id": "c1", "text": "alpha body", "doc_title": "alpha", "page": 1},
                {"chunk_id": "c2", "text": "beta body", "doc_title": "beta", "page": 3},
            ]
        }
    )
    with patch("harbor_clerk.llm.research.execute_tool", new=AsyncMock(return_value=read_result)):
        passages_text, evidence_docs = await _read_evidence(
            coverage, user_id=None, max_passages=10, context_budget_chars=100_000
        )
    assert "alpha body" in passages_text and "beta body" in passages_text
    by_id = {d["doc_id"]: d for d in evidence_docs}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"]["doc_title"] == "alpha"
    assert by_id["d1"]["page"] == 1
    assert by_id["d2"]["page"] == 3


@pytest.mark.asyncio
async def test_read_evidence_excludes_budget_truncated_passages_from_evidence_docs():
    """A passage skipped because it would exceed context_budget_chars must
    NOT appear in evidence_docs — evidence_docs tracks only the passages
    that actually made it into passages_text."""
    coverage = {
        "c1": {"doc_id": "d1", "doc_title": "alpha", "page": 1, "score": 2.0, "snippet": "a"},
        "c2": {"doc_id": "d2", "doc_title": "beta", "page": 2, "score": 1.5, "snippet": "b"},
    }
    big = "x" * 400
    read_result = json.dumps(
        {
            "passages": [
                {"chunk_id": "c1", "text": big, "doc_title": "alpha", "page": 1},
                {"chunk_id": "c2", "text": "beta body", "doc_title": "beta", "page": 2},
            ]
        }
    )
    with patch("harbor_clerk.llm.research.execute_tool", new=AsyncMock(return_value=read_result)):
        passages_text, evidence_docs = await _read_evidence(
            coverage, user_id=None, max_passages=10, context_budget_chars=450
        )
    # The big first passage fits; the second is over budget and skipped.
    assert big in passages_text
    assert "beta body" not in passages_text
    assert {d["doc_id"] for d in evidence_docs} == {"d1"}
