"""REST Find All route contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.routes import search as search_route
from harbor_clerk.api.schemas.search import FindAllRequest, FindAllResponse
from harbor_clerk.search_types import FindAllHit, FindAllResult
from harbor_clerk.source_ref import SourceRef


def test_search_find_all_route_is_registered() -> None:
    assert any(
        getattr(route, "path", None) == "/search/find-all" and "POST" in getattr(route, "methods", set())
        for route in search_route.router.routes
    )


@pytest.mark.anyio
async def test_search_find_all_forwards_filters_and_returns_sources(monkeypatch) -> None:
    doc_id = uuid4()
    chunk_id = uuid4()
    captured: dict[str, object] = {}

    async def fake_find_all(session, query, **kwargs):
        captured["session"] = session
        captured["query"] = query
        captured.update(kwargs)
        return FindAllResult(
            hits=[
                FindAllHit(
                    doc_id=str(doc_id),
                    doc_title="Vendor Contract",
                    mime_type="application/pdf",
                    language="en",
                    score=0.87,
                    ingested_at=datetime(2026, 6, 1, tzinfo=UTC),
                    page_range="2",
                    top_chunk_id=str(chunk_id),
                    top_chunk_text="Force majeure clause excerpt.",
                    top_chunk_page=2,
                    top_chunk_heading="Terms",
                )
            ],
            total_matches=1,
            offset=5,
            truncated=False,
            sort_by="date_desc",
            presentation="full",
        )

    class SourceContext:
        def ref_for_doc(self, doc_or_id, *, chunk_id=None, pages=None, section=None, include_relative_path=True):
            return SourceRef(
                doc_id=str(doc_or_id),
                doc_title="Vendor Contract",
                chunk_id=str(chunk_id),
                pages=pages,
                section=section,
                source_kind="document",
                source_label="Vendor Contract",
                folder_label="Contracts",
                relative_path="vendors/contract.pdf",
                citation="Vendor Contract, p. 2",
            )

    async def fake_load_source_ref_context(session, doc_ids):
        captured["source_doc_ids"] = list(doc_ids)
        return SourceContext()

    monkeypatch.setattr(search_route, "find_all", fake_find_all)
    monkeypatch.setattr(search_route, "load_source_ref_context", fake_load_source_ref_context)

    session = object()
    response = await search_route.search_find_all(
        FindAllRequest(
            query="vendor contract",
            text_contains="force majeure",
            max_results=10,
            offset=5,
            presentation="full",
            sort_by="date_desc",
            doc_ids=[doc_id],
            language="en",
            mime_type="application/pdf",
            metadata_filter={"sidecar.vendor": "Pinnacle"},
            summary_state="has",
            pipeline_status="ready",
            job_stage="summarize",
            job_status="done",
            job_issue="summary_pending",
        ),
        principal=Principal(type="user", id=uuid4(), role="admin"),
        session=session,
    )

    assert isinstance(response, FindAllResponse)
    assert captured["session"] is session
    assert captured["query"] == "vendor contract"
    assert captured["max_results"] == 10
    assert captured["offset"] == 5
    assert captured["presentation"] == "full"
    assert captured["sort_by"] == "date_desc"
    assert captured["text_contains"] == "force majeure"
    assert captured["doc_ids"] == [doc_id]
    assert captured["language"] == "en"
    assert captured["mime_type"] == "application/pdf"
    assert captured["metadata_filter"] == {"sidecar.vendor": "Pinnacle"}
    assert captured["summary_state"] == "has"
    assert captured["pipeline_status"] == "ready"
    assert captured["job_stage"] == "summarize"
    assert captured["job_status"] == "done"
    assert captured["job_issue"] == "summary_pending"
    assert captured["source_doc_ids"] == [str(doc_id)]

    assert response.total_matches == 1
    assert response.returned == 1
    hit = response.results[0]
    assert hit.doc_id == str(doc_id)
    assert hit.citation == "Vendor Contract, p. 2"
    assert hit.source is not None
    assert hit.source.folder_label == "Contracts"
    assert hit.source.relative_path == "vendors/contract.pdf"
    assert hit.top_chunk is not None
    assert hit.top_chunk.chunk_id == str(chunk_id)
    assert hit.top_chunk.text == "Force majeure clause excerpt."


@pytest.mark.anyio
async def test_search_find_all_clamps_max_results(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Settings:
        find_all_max_results_cap = 25

    async def fake_find_all(session, query, **kwargs):
        captured.update(kwargs)
        return FindAllResult(
            hits=[],
            total_matches=0,
            offset=0,
            truncated=False,
            sort_by="relevance",
            presentation="brief",
        )

    async def fake_load_source_ref_context(session, doc_ids):
        raise AssertionError("empty find-all results should not need source context")

    monkeypatch.setattr(search_route, "get_settings", lambda: Settings())
    monkeypatch.setattr(search_route, "find_all", fake_find_all)
    monkeypatch.setattr(search_route, "load_source_ref_context", fake_load_source_ref_context)

    response = await search_route.search_find_all(
        FindAllRequest(query="vendor contract", max_results=999),
        principal=Principal(type="user", id=uuid4(), role="admin"),
        session=object(),
    )

    assert response.results == []
    assert captured["max_results"] == 25


@pytest.mark.anyio
async def test_search_find_all_returns_422_for_invalid_filter(monkeypatch) -> None:
    async def fake_find_all(*args, **kwargs):
        raise ValueError("metadata_filter keys must be exactly 'namespace.key'")

    monkeypatch.setattr(search_route, "find_all", fake_find_all)

    with pytest.raises(HTTPException) as exc_info:
        await search_route.search_find_all(
            FindAllRequest(query="vendor contract", metadata_filter={"too.many.dots": "x"}),
            principal=Principal(type="user", id=uuid4(), role="admin"),
            session=object(),
        )

    assert exc_info.value.status_code == 422
    assert "metadata_filter keys" in exc_info.value.detail


def test_find_all_request_rejects_doc_id_and_doc_ids() -> None:
    doc_id = uuid4()
    with pytest.raises(ValidationError):
        FindAllRequest.model_validate(
            {
                "query": "vendor contract",
                "doc_id": str(doc_id),
                "doc_ids": [str(doc_id)],
            }
        )
