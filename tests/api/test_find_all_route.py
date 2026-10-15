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

    async def fake_load_source_ref_context(session, doc_ids, **kwargs):
        captured["source_doc_ids"] = list(doc_ids)
        captured["folder_label_from_path"] = kwargs.get("folder_label_from_path")
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
    assert captured["folder_label_from_path"] is True  # a signed-in user keeps the folder's on-disk leaf

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

    async def fake_load_source_ref_context(session, doc_ids, **kwargs):
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


def test_find_all_response_schema_includes_snippet() -> None:
    """#820: the REST schema (and so the OpenAPI document) carries the brief snippet."""
    from harbor_clerk.api.schemas.search import FindAllHitOut

    assert "snippet" in FindAllHitOut.model_fields
    assert "snippet" in FindAllResponse.model_json_schema()["$defs"]["FindAllHitOut"]["properties"]


async def test_find_all_route_returns_brief_snippets_from_postgres(client, admin_token, db_session) -> None:
    """POST /api/search/find-all over a real database: brief rows carry a bounded snippet
    centred on text_contains; full rows carry the whole top chunk and no snippet."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import _FIND_ALL_SNIPPET_MAX_CHARS
    from tests.conftest import auth_header

    filler = "The supplier shall deliver the goods described in the schedule on the agreed dates. "
    text = filler * 6 + "Either party may terminate for convenience on thirty days notice. " + filler * 6
    doc = Document(
        title="Supply Agreement",
        status="active",
        sha256=b"sha_rest_snippet_0000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en"))
    await db_session.flush()

    body = {"query": "terminate", "text_contains": "terminate for convenience"}
    resp = await client.post("/api/search/find-all", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text[:300]
    (row,) = resp.json()["results"]
    assert row["top_chunk"] is None
    assert len(row["snippet"]) <= _FIND_ALL_SNIPPET_MAX_CHARS
    assert "terminate for convenience" in row["snippet"]
    assert row["citation"]

    resp = await client.post(
        "/api/search/find-all", json={**body, "presentation": "full"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200, resp.text[:300]
    (row,) = resp.json()["results"]
    assert row["snippet"] is None
    assert row["top_chunk"]["text"] == text


@pytest.mark.parametrize("cap", [50, 0])
async def test_find_all_route_capped_key_gets_the_chunk_prefix(client, db_session, cap) -> None:
    """Over REST, a key with max_snippet_chars gets the chunk's prefix within its cap, not a window
    on text_contains; a cap of 0 omits the snippet and its chunk id. As on kb_find_all."""
    from harbor_clerk.auth import generate_api_key, hash_api_key
    from harbor_clerk.models import ApiKey, Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from tests.conftest import auth_header

    filler = "The supplier shall deliver the goods described in the schedule on the agreed dates. "
    doc = Document(
        title="Capped Agreement",
        status="active",
        sha256=f"sha_rest_snippet_cap_{cap:012d}".encode(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    text = filler * 6 + "Either party may terminate for convenience on thirty days notice. " + filler * 6
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en"))
    raw_key = generate_api_key()
    db_session.add(
        ApiKey(name="capped", key_hash=hash_api_key(raw_key), permission_tier="search", max_snippet_chars=cap)
    )
    await db_session.flush()

    body = {"query": "terminate", "text_contains": "terminate for convenience"}
    resp = await client.post("/api/search/find-all", json=body, headers=auth_header(raw_key))
    assert resp.status_code == 200, resp.text[:300]
    (row,) = resp.json()["results"]
    if cap == 0:
        assert row["snippet"] is None
        assert row["source"].get("chunk_id") is None
        return
    assert 0 < len(row["snippet"]) <= cap
    assert "terminate for convenience" not in row["snippet"]
    assert " ".join(text[:cap].split()).startswith(row["snippet"].removesuffix("\u2026"))
    assert row["source"]["chunk_id"]


@pytest.mark.anyio
async def test_search_find_all_source_chunk_id_without_source_context(monkeypatch) -> None:
    """A brief hit with no SourceRef context still names the snippet's chunk (the fallback source ref)."""
    snippet_chunk_id = str(uuid4())

    async def fake_find_all(session, query, **kwargs):
        hit = FindAllHit(
            doc_id=str(uuid4()),
            doc_title="Orphan",
            mime_type="text/plain",
            language="en",
            score=0.5,
            ingested_at=datetime(2026, 6, 1, tzinfo=UTC),
            page_range="3",
            top_chunk_id=None,
            top_chunk_text=None,
            top_chunk_page=None,
            top_chunk_heading=None,
            snippet="an orphan passage",
            snippet_chunk_id=snippet_chunk_id,
        )
        return FindAllResult(
            hits=[hit], total_matches=1, offset=0, truncated=False, sort_by="relevance", presentation="brief"
        )

    async def no_source_context(session, doc_ids, **kwargs):
        return None

    monkeypatch.setattr(search_route, "find_all", fake_find_all)
    monkeypatch.setattr(search_route, "load_source_ref_context", no_source_context)

    response = await search_route.search_find_all(
        FindAllRequest(query="orphan"),
        principal=Principal(type="user", id=uuid4(), role="admin"),
        session=object(),
    )
    (row,) = response.results
    assert row.snippet == "an orphan passage"
    assert row.source is not None and row.source.chunk_id == snippet_chunk_id
