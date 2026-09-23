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
    """Seven queries are two batches; one query paginates and stops after one page: three searches, each
    reported as it completes. Planned starts at the most that could run (2 batches + 2 pages for each of the
    7 queries), drops to 4 once only one query paginates, and to 3 when its first page ends it."""
    queries = [f"q{i}" for i in range(7)]
    with patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(paginated_query="q1")):
        steps = [s async for s in research_module._search_fan_out_steps(queries, None, 20, paginate=True)]

    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 16), (2, 16), (3, 3)]
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
async def test_fan_out_steps_follow_a_query_through_both_extra_pages():
    """A query that still has more after its first extra page gets the second. One batch and two pages: planned
    settles at three once the other query turns out not to paginate, and stays there."""
    inner = _fake_execute_tool(paginated_query="q1")

    async def two_pages(name, args, user_id, *, mode=None, user_scope=None):
        if name == "search_documents":
            first = args["offset"] == 20
            return json.dumps({"hits": [_hit(f"chunk-q1-page-{args['offset']}")], "has_more": first})
        return await inner(name, args, user_id, mode=mode, user_scope=user_scope)

    with patch.object(research_module, "execute_tool", side_effect=two_pages):
        steps = [s async for s in research_module._search_fan_out_steps(["q0", "q1"], None, 20, paginate=True)]
    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 5), (2, 3), (3, 3)]
    assert {"chunk-q1-page-20", "chunk-q1-page-21"} <= set(steps[-1][1])


@pytest.mark.asyncio
async def test_a_page_that_raises_ends_its_query_and_is_still_a_search_that_ran():
    inner = _fake_execute_tool(paginated_query="q1")

    async def failing_page(name, args, user_id, *, mode=None, user_scope=None):
        if name == "search_documents":
            raise RuntimeError("page fetch failed")
        return await inner(name, args, user_id, mode=mode, user_scope=user_scope)

    with patch.object(research_module, "execute_tool", side_effect=failing_page):
        steps = [s async for s in research_module._search_fan_out_steps(["q0", "q1"], None, 20, paginate=True)]
    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 5), (2, 2)]  # the second page is no longer planned
    assert set(steps[-1][1]) == {"chunk-q0", "chunk-q1"}


@pytest.mark.asyncio
async def test_fan_out_without_pagination_plans_only_its_batches():
    with patch.object(research_module, "execute_tool", side_effect=_fake_execute_tool(paginated_query="q1")):
        steps = [s async for s in research_module._search_fan_out_steps([f"q{i}" for i in range(7)], None, 20)]
    assert [s[1:] for s in steps if s[0] == "progress"] == [(1, 2), (2, 2)]


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
    # Standard depth paginates, so planned starts at 2 batches + 2 pages for each of the 7 queries.
    assert [(e["searches_done"], e["searches_planned"]) for e in searching[:2]] == [(1, 16), (2, 16)]
    assert all(e["phase"] == "searching" and e["step"] == 2 for e in searching[:2])


@pytest.mark.asyncio
async def test_research_stream_heartbeats_and_reports_during_the_gap_round_too(
    db_session,
    admin_user,
    unscoped_research_state,  # noqa: F811
    mock_research_session_factory,  # noqa: F811
):
    """The gap round is the second call site of the fan-out. With findings in hand and six gap queries (two
    batches), the second gap batch must see a heartbeat newer than the first gap batch did (only the gap
    round's own heartbeat can be between them), the stream must emit step-5 searching events that carry the
    round, and the round must reach its result (the coverage step is consumed, not indexed)."""
    from unittest.mock import AsyncMock

    from harbor_clerk.models.research_state import ResearchState

    conv, _state = unscoped_research_state
    heartbeats_seen = []
    inner = _fake_execute_tool()  # hits present, so the gap round is reachable

    async def fake_execute_tool(name, args, user_id, *, mode=None, user_scope=None):
        if name == "batch_search":
            async with research_module.async_session_factory() as session:
                fresh = await session.get(ResearchState, conv.conversation_id)
                heartbeats_seen.append(fresh.heartbeat_at)
            await asyncio.sleep(0.01)
        return await inner(name, args, user_id, mode=mode, user_scope=user_scope)

    mock_client = _make_mock_httpx_client(
        planning_resp=_make_planning_response(["termination notice period clause"]),
        note_resp=_make_note_extraction_response(),
        synthesis_lines=_make_synthesis_stream_response(),
    )
    with (
        patch.object(research_module, "execute_tool", side_effect=fake_execute_tool),
        patch.object(research_module, "_extract_notes_with_retry", new=AsyncMock(return_value="Notes.")),
        patch.object(
            research_module,
            "_check_gaps",
            new=AsyncMock(side_effect=[[f"governing law of agreement {i}" for i in range(6)], []]),
        ),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        raw = [e async for e in research_module.research_stream(conv.conversation_id, user_id=admin_user.user_id)]

    assert len(heartbeats_seen) == 3, heartbeats_seen  # one phase-2 batch, two gap-round batches
    assert heartbeats_seen[2] > heartbeats_seen[1], "no heartbeat between the gap round's two batches"
    events = _events(raw)
    gap_events = [e for e in events if e.get("type") == "progress" and e.get("step") == 5 and "searches_done" in e]
    assert [(e["searches_done"], e["searches_planned"], e["round"]) for e in gap_events] == [(1, 2, 1), (2, 2, 1)]
    assert any(e.get("type") == "notes" and "Gap search found" in e.get("content", "") for e in events), (
        "the gap round did not reach its result"
    )
