"""Tests for ResearchDetail.citations on GET /api/research/{conv_id}.

The research route computes citations at read-time from the persisted
``ChatMessage(role='tool')`` rows of the turn — the same data the
``citations_extract`` module knows how to parse. These tests verify the
wire-up: seed a finished research with tool messages, fetch detail, expect
deduped citations on the response.
"""

from __future__ import annotations

import json
import uuid

from harbor_clerk.models import ChatMessage, Conversation
from harbor_clerk.models.research_state import ResearchState
from tests.conftest import auth_header


async def _seed_research(
    db_session,
    admin_user,
    *,
    status: str = "completed",
    tool_messages: list[tuple[str, str]] | None = None,
) -> uuid.UUID:
    """Insert a conversation + research_state + (optionally) tool messages.

    ``tool_messages`` is a list of (tool_call_id, raw_result_json_str). The
    helper creates one assistant message with matching tool_calls plus one
    tool result message per entry.
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
        current_round=2,
        max_rounds=4,
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

    if tool_messages:
        for tc_id, raw in tool_messages:
            db_session.add(
                ChatMessage(
                    conversation_id=conv_id,
                    role="tool",
                    tool_call_id=tc_id,
                    content=raw,
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


async def test_research_detail_citations_empty_when_no_tools(client, admin_user, admin_token, db_session):
    conv_id = await _seed_research(db_session, admin_user, tool_messages=None)
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["citations"] == []


async def test_research_detail_citations_from_kb_search(client, admin_user, admin_token, db_session):
    raw = json.dumps(
        {
            "hits": [
                {
                    "doc_id": "11111111-1111-1111-1111-111111111111",
                    "chunk_id": "aaa",
                    "doc_title": "Doc A",
                    "score": 0.9,
                    "pages": "1-2",
                },
                {
                    "doc_id": "22222222-2222-2222-2222-222222222222",
                    "chunk_id": "bbb",
                    "doc_title": "Doc B",
                    "score": 0.7,
                },
            ]
        }
    )
    conv_id = await _seed_research(db_session, admin_user, tool_messages=[("call_1", raw)])
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    cites = resp.json()["citations"]
    assert {c["doc_id"] for c in cites} == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }
    a = next(c for c in cites if c["doc_id"] == "11111111-1111-1111-1111-111111111111")
    assert a["chunk_id"] == "aaa"
    assert a["doc_title"] == "Doc A"
    assert a["score"] == 0.9
    assert a["pages"] == "1-2"


async def test_research_detail_citations_deduped_across_tool_results(client, admin_user, admin_token, db_session):
    # Same chunk surfaces in two searches → dedupe to one entry, with the
    # higher score winning (matches the dedupe_citations contract).
    r1 = json.dumps({"hits": [{"doc_id": "D1", "chunk_id": "C1", "score": 0.5}]})
    r2 = json.dumps(
        {
            "hits": [
                {"doc_id": "D1", "chunk_id": "C1", "score": 0.85},
                {"doc_id": "D2", "chunk_id": "C2", "score": 0.6},
            ]
        }
    )
    conv_id = await _seed_research(
        db_session,
        admin_user,
        tool_messages=[("call_1", r1), ("call_2", r2)],
    )
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    cites = resp.json()["citations"]
    assert len(cites) == 2
    by_chunk = {c["chunk_id"]: c for c in cites}
    assert by_chunk["C1"]["score"] == 0.85
    assert by_chunk["C2"]["score"] == 0.6


async def test_research_detail_citations_skips_error_payloads(client, admin_user, admin_token, db_session):
    # A failed tool call returns {"error": ...}. Those must not produce
    # citations — the extractor is best-effort and treats errors as no-ops.
    err = json.dumps({"error": "no such doc_id"})
    ok = json.dumps({"hits": [{"doc_id": "D1", "chunk_id": "C1"}]})
    conv_id = await _seed_research(
        db_session,
        admin_user,
        tool_messages=[("call_err", err), ("call_ok", ok)],
    )
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    cites = resp.json()["citations"]
    assert len(cites) == 1
    assert cites[0]["doc_id"] == "D1"


async def test_research_detail_citations_from_read_passages_keeps_chunk_only(
    client, admin_user, admin_token, db_session
):
    # kb_read_passages doesn't include doc_id. Those chunk-only entries still
    # appear in citations so the UI can show them; the test harness ignores
    # entries without doc_id for citation_overlap.
    raw = json.dumps(
        {
            "passages": [
                {"chunk_id": "C1", "doc_title": "X", "pages": "5", "text": "..."},
            ]
        }
    )
    conv_id = await _seed_research(db_session, admin_user, tool_messages=[("call_1", raw)])
    resp = await client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    cites = resp.json()["citations"]
    assert len(cites) == 1
    assert "doc_id" not in cites[0]
    assert cites[0]["chunk_id"] == "C1"
    assert cites[0]["doc_title"] == "X"
