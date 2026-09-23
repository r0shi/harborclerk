"""The research search fan-out reports after every search, and the stream heartbeats on each report (#700).

Before this, the phase ran up to 35 searches between two heartbeats and two SSE events. At 3-4 s a search
that was 2 minutes; with the GPU paging (#698) it was 6 to 10 minutes, and both the API's reaper (five
minutes without a heartbeat) and the eval harness (325 s without an event) killed research that was still
working.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from harbor_clerk.llm import research as research_module
from tests.test_research_engine_scope_threading import (  # noqa: F401 — fixtures
    _make_mock_httpx_client,
    _make_note_extraction_response,
    _make_planning_response,
    _make_synthesis_stream_response,
    mock_research_session_factory,
    unscoped_research_state,
)


def _hit(chunk_id: str) -> dict:
    return {"chunk_id": chunk_id, "doc_id": "doc-1", "doc_title": "Doc", "score": 0.5, "text": "passage"}


def _fake_execute_tool(paginated_query: str | None = None, calls: list | None = None, *, hits: bool = True):
    """batch_search returns one hit per query (none with hits=False); `paginated_query` reports more, and its
    first extra page ends it."""

    async def fake(name, args, user_id, *, mode=None, user_scope=None):
        if calls is not None:
            calls.append(name)
        if name == "batch_search":
            return json.dumps(
                {
                    "results": [
                        {
                            "query": q,
                            "hits": [_hit(f"chunk-{q}")] if hits else [],
                            "has_more": q == paginated_query,
                            "total_candidates": 50,
                        }
                        for q in args["queries"]
                    ]
                }
            )
        if name == "search_documents":
            return json.dumps({"hits": [_hit(f"chunk-{args['query']}-page")] if hits else [], "has_more": False})
        return json.dumps({})

    return fake


@pytest.mark.asyncio
async def test_fan_out_steps_report_after_every_search_then_the_coverage():
    """Seven queries are two batches; one query paginates and stops after one page: three searches,
    each reported as it completes, the planned count corrected when the second page is not needed."""
    queries = [f"q{i}" for i in range(7)]
    with patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(paginated_query="q1")):
        steps = [s async for s in research_module._search_fan_out_steps(queries, None, 20, paginate=True)]

    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 2), (2, 2), (3, 3)]
    assert steps[-1][0] == "coverage"
    coverage = steps[-1][1]
    assert set(coverage) == {f"chunk-q{i}" for i in range(7)} | {"chunk-q1-page"}
    assert coverage["chunk-q1"]["queries"] == ["q1"]  # sets become lists before the coverage is handed over


@pytest.mark.asyncio
async def test_fan_out_steps_count_a_failed_batch_as_a_search_that_ran():
    """A batch that raises is logged and skipped, as before; it still advances the count so the caller's
    heartbeat happens, which is the point of reporting."""

    async def failing(name, args, user_id, *, mode=None, user_scope=None):
        raise RuntimeError("search is down")

    with patch.object(research_module, "execute_tool", side_effect=failing):
        steps = [s async for s in research_module._search_fan_out_steps(["a", "b", "c", "d", "e", "f"], None, 20)]
    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 2), (2, 2)]
    assert steps[-1] == ("coverage", {})


@pytest.mark.asyncio
async def test_search_fan_out_is_the_drained_steps():
    """The old entry point returns exactly the coverage the generator ends with."""
    queries = [f"q{i}" for i in range(7)]
    with patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(paginated_query="q1")):
        drained = await research_module._search_fan_out(queries, None, 20, paginate=True)
        steps = [s async for s in research_module._search_fan_out_steps(queries, None, 20, paginate=True)]
    assert drained == steps[-1][1]


def _events(raw: list[str]) -> list[dict]:
    out = []
    for chunk in raw:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


@pytest.mark.asyncio
async def test_research_stream_heartbeats_and_reports_between_search_batches(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811 — the imported fixture, requested by name
    mock_research_session_factory,  # noqa: F811
):
    """Seven planned queries are two batch_search calls. The second call must find a heartbeat newer than
    the first did, and the stream must have emitted a searching progress event per batch."""
    from harbor_clerk.models.research_state import ResearchState

    conv, _state = unscoped_research_state
    heartbeats_seen = []
    # No hits: the shared LLM mock serves the no-findings path, and the heartbeat does not depend on findings.
    inner = _fake_execute_tool(hits=False)

    async def fake_execute_tool(name, args, user_id, *, mode=None, user_scope=None):
        if name == "batch_search":
            async with research_module.async_session_factory() as session:
                fresh = await session.get(ResearchState, conv.conversation_id)
                heartbeats_seen.append(fresh.heartbeat_at)
            await asyncio.sleep(0.01)  # so consecutive heartbeats cannot tie
        return await inner(name, args, user_id, mode=mode, user_scope=user_scope)

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response([f"termination notice period, clause {i}" for i in range(7)]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response(),
    )
    with (
        patch.object(research_module, "execute_tool", side_effect=fake_execute_tool),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]

    assert len(heartbeats_seen) == 2, heartbeats_seen
    assert heartbeats_seen[1] > heartbeats_seen[0], "the heartbeat did not move between the two batches"

    searching = [e for e in _events(raw) if e.get("type") == "progress" and "searches_done" in e]
    assert [(e["searches_done"], e["searches_planned"]) for e in searching[:2]] == [(1, 2), (2, 2)]
    assert all(e["phase"] == "searching" and e["step"] == 2 for e in searching[:2])
