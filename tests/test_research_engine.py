"""Unit tests for research engine internals (harbor_clerk.llm.research)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.llm.research import _plan_queries

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
