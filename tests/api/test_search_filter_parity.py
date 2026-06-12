"""REST search filter parity tests."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.routes import search as search_route
from harbor_clerk.api.schemas.search import SearchRequest, SearchResponse
from harbor_clerk.search_types import SearchResult


@pytest.mark.anyio
async def test_rest_search_forwards_metadata_filter_and_text_contains(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_hybrid_search(session, query, **kwargs):
        captured["session"] = session
        captured["query"] = query
        captured.update(kwargs)
        return SearchResult(hits=[], total_candidates=0, reranker_status="disabled")

    monkeypatch.setattr(search_route, "hybrid_search", fake_hybrid_search)

    session = object()
    principal = Principal(type="user", id=uuid4(), role="admin")
    request = SearchRequest(
        query="vendor contract",
        metadata_filter={
            "email.from_address": "alice@example.com",
            "sidecar.vendor": "Pinnacle Tech Solutions",
        },
        text_contains="force majeure",
        summary_state="missing",
        pipeline_status="processing",
        job_stage="entities",
        job_status="done",
        job_issue="entity_skipped",
    )

    response = await search_route.search(request, principal=principal, session=session)

    assert isinstance(response, SearchResponse)
    assert captured["session"] is session
    assert captured["query"] == "vendor contract"
    assert captured["metadata_filter"] == {
        "email.from_address": "alice@example.com",
        "sidecar.vendor": "Pinnacle Tech Solutions",
    }
    assert captured["text_contains"] == "force majeure"
    assert captured["summary_state"] == "missing"
    assert captured["pipeline_status"] == "processing"
    assert captured["job_stage"] == "entities"
    assert captured["job_status"] == "done"
    assert captured["job_issue"] == "entity_skipped"


@pytest.mark.anyio
async def test_rest_search_returns_422_for_invalid_filter(monkeypatch) -> None:
    async def fake_hybrid_search(*args, **kwargs):
        raise ValueError("metadata_filter keys must be exactly 'namespace.key'")

    monkeypatch.setattr(search_route, "hybrid_search", fake_hybrid_search)

    with pytest.raises(HTTPException) as exc_info:
        await search_route.search(
            SearchRequest(query="vendor contract", metadata_filter={"too.many.dots": "x"}),
            principal=Principal(type="user", id=uuid4(), role="admin"),
            session=object(),
        )

    assert exc_info.value.status_code == 422
    assert "metadata_filter keys" in exc_info.value.detail


def test_search_request_rejects_non_object_metadata_filter() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(
            {
                "query": "vendor contract",
                "metadata_filter": ["email.from_address", "alice@example.com"],
            }
        )
