"""Tests for ResearchDetail.citations on GET /api/research/{conv_id}.

Citations are persisted on ``research_state.citations`` by the research engine
and surfaced verbatim (after dedupe_citations) by the detail endpoint. These
tests verify that wire-up: seed a ResearchState with citations, fetch detail,
expect those citations on the response.
"""

from __future__ import annotations

import uuid

from harbor_clerk.models import ChatMessage, Conversation
from harbor_clerk.models.research_state import ResearchState
from tests.conftest import auth_header


async def _seed_research(
    db_session,
    admin_user,
    *,
    status: str = "completed",
    error: str | None = None,
    citations: list[dict] | None = None,
) -> uuid.UUID:
    """Insert a conversation + research_state + optional persisted citations.

    ``citations`` is stored directly on ``research_state.citations`` — matching
    what the research engine writes at completion time.
    """
    conv_id = uuid.uuid4()
    conv = Conversation(
        conversation_id=conv_id,
        user_id=admin_user.user_id,
        title="test research",
        mode="research",
    )
    db_session.add(conv)
    await db_session.flush()  # make FK visible to subsequent inserts
    state = ResearchState(
        conversation_id=conv_id,
        strategy="search",
        status=status,
        error=error,
        current_round=2,
        max_rounds=4,
        citations=citations,
    )
    db_session.add(state)

    # User question
    db_session.add(
        ChatMessage(
            conversation_id=conv_id,
            role="user",
            content="What's in the corpus?",
        )
    )

    if status == "completed":
        db_session.add(
            ChatMessage(
                conversation_id=conv_id,
                role="assistant",
                content="The report.",
                model_id="test-model",
            )
        )

    await db_session.flush()
    return conv_id


async def test_research_detail_citations_empty_when_no_citations(client, admin_user, admin_token, db_session):
    # No citations persisted on state → empty list on the wire.
    conv_id = await _seed_research(db_session, admin_user, citations=None)
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["citations"] == []


async def test_research_detail_citations_multiple_docs(client, admin_user, admin_token, db_session):
    # Multiple docs persisted by the engine are all surfaced.
    cites = [
        {
            "doc_id": "11111111-1111-1111-1111-111111111111",
            "doc_title": "Doc A",
            "page": 1,
        },
        {
            "doc_id": "22222222-2222-2222-2222-222222222222",
            "doc_title": "Doc B",
            "page": 3,
        },
    ]
    conv_id = await _seed_research(db_session, admin_user, citations=cites)
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    returned = resp.json()["citations"]
    assert {c["doc_id"] for c in returned} == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }
    a = next(c for c in returned if c["doc_id"] == "11111111-1111-1111-1111-111111111111")
    assert a["doc_title"] == "Doc A"
    assert a["page"] == 1


async def test_research_detail_citations_deduped_by_doc_id(client, admin_user, admin_token, db_session):
    # dedupe_citations collapses duplicate doc_id entries (same doc, different
    # pages) — the route passes state.citations through dedupe_citations.
    cites = [
        {"doc_id": "D1", "doc_title": "Doc 1", "page": 1},
        {"doc_id": "D1", "doc_title": "Doc 1", "page": 2},
        {"doc_id": "D2", "doc_title": "Doc 2", "page": 5},
    ]
    conv_id = await _seed_research(db_session, admin_user, citations=cites)
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    returned = resp.json()["citations"]
    # After dedupe: D1 appears once, D2 once
    doc_ids = [c["doc_id"] for c in returned]
    assert doc_ids.count("D1") == 1
    assert doc_ids.count("D2") == 1


async def test_research_detail_citations_single_doc(client, admin_user, admin_token, db_session):
    # Single citation roundtrips cleanly.
    cites = [{"doc_id": "D1", "doc_title": "Solo Doc", "page": 7}]
    conv_id = await _seed_research(db_session, admin_user, citations=cites)
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    returned = resp.json()["citations"]
    assert len(returned) == 1
    assert returned[0]["doc_id"] == "D1"
    assert returned[0]["doc_title"] == "Solo Doc"
    assert returned[0]["page"] == 7


async def test_research_detail_citations_empty_list_in_state(client, admin_user, admin_token, db_session):
    # Explicit empty list on state → empty list on the wire (not None).
    conv_id = await _seed_research(db_session, admin_user, citations=[])
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["citations"] == []


# ── error field ──


async def test_research_detail_error_field_null_on_success(client, admin_user, admin_token, db_session):
    conv_id = await _seed_research(db_session, admin_user, status="completed")
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["error"] is None


async def test_research_detail_error_field_surfaces_reaper_reason(client, admin_user, admin_token, db_session):
    # The reaper at app.py:154 sets this exact string when heartbeat goes stale.
    conv_id = await _seed_research(
        db_session,
        admin_user,
        status="interrupted",
        error="Research task stalled — no progress for 5+ minutes",
    )
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["error"] == "Research task stalled — no progress for 5+ minutes"


async def test_research_detail_error_field_surfaces_synthesis_failure(client, admin_user, admin_token, db_session):
    # research.py:1342 sets this when llama-server returns an HTTP error during
    # the final synthesis pass.
    conv_id = await _seed_research(
        db_session,
        admin_user,
        status="interrupted",
        error="Synthesis failed: LLM error (502)",
    )
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["error"] == "Synthesis failed: LLM error (502)"


async def test_research_detail_surfaces_state_citations(db_session, client, admin_user, admin_token):
    """result.citations is populated from research_state.citations."""
    import uuid as _uuid

    conv_id = _uuid.uuid4()
    conv = Conversation(
        conversation_id=conv_id,
        user_id=admin_user.user_id,
        title="cites",
        mode="research",
    )
    db_session.add(conv)
    await db_session.flush()
    cites = [{"doc_id": "d-1", "doc_title": "alpha", "page": 2}]
    db_session.add(
        ResearchState(
            conversation_id=conv_id,
            strategy="search",
            status="completed",
            max_rounds=500,
            citations=cites,
        )
    )
    # Add user message so the route can find `question`
    db_session.add(
        ChatMessage(
            conversation_id=conv_id,
            role="user",
            content="What is in the corpus?",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["citations"] == cites
