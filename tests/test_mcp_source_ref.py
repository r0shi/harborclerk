"""MCP SourceRef contract tests for high-volume result tools."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import (
    _mcp_principal,
    kb_batch_search,
    kb_corpus_overview,
    kb_document_outline,
    kb_documents_by_date,
    kb_expand_context,
    kb_find_all,
    kb_find_related,
    kb_get_document,
    kb_list_recent,
    kb_read_passages,
    kb_search,
    kb_verify_identifier,
)
from harbor_clerk.models import Chunk, Document, DocumentLink
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.search import FindAllHit, FindAllResult, SearchHit, SearchResult


@pytest.fixture
def mcp_principal(admin_user):
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    yield
    _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


def _doc(**kwargs) -> Document:
    defaults = {
        "title": "NDA",
        "canonical_filename": "nda.pdf",
        "status": "active",
        "sha256": b"x" * 32,
        "pipeline_status": PipelineStatus.ready,
        "mime_type": "application/pdf",
        "source_path": "/Users/alex/private/contracts/client-a/nda.pdf",
        "doc_metadata": {},
    }
    defaults.update(kwargs)
    return Document(**defaults)


async def _add_watched_doc(db_session: AsyncSession, doc: Document, relative_path: str = "client-a/nda.pdf") -> None:
    folder = WatchedFolder(path="/Users/alex/private/contracts", display_name="Contracts")
    db_session.add(folder)
    await db_session.flush()

    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=relative_path,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.flush()


def _search_hit(doc: Document, *, chunk_id: uuid.UUID | None = None) -> SearchHit:
    return SearchHit(
        chunk_id=str(chunk_id or uuid.uuid4()),
        doc_id=str(doc.doc_id),
        chunk_num=0,
        chunk_text="The NDA terminates on written notice.",
        page_start=4,
        page_end=5,
        language="en",
        ocr_used=False,
        ocr_confidence=None,
        score=0.91,
        doc_title=doc.title,
    )


async def test_kb_search_hit_includes_source_ref(db_session, mcp_principal, mock_session_factory, monkeypatch) -> None:
    doc = _doc()
    await _add_watched_doc(db_session, doc)
    hit = _search_hit(doc)

    async def fake_hybrid_search(*args, **kwargs):
        return SearchResult(hits=[hit], total_candidates=1)

    monkeypatch.setattr("harbor_clerk.mcp_server.hybrid_search", fake_hybrid_search)

    payload = json.loads(await kb_search("termination"))
    result_hit = payload["hits"][0]

    assert result_hit["citation"] == "NDA, pp. 4-5"
    assert result_hit["source"]["doc_id"] == str(doc.doc_id)
    assert result_hit["source"]["chunk_id"] == hit.chunk_id
    assert result_hit["source"]["folder_label"] == "Contracts"
    assert result_hit["source"]["relative_path"] == "client-a/nda.pdf"
    assert "/Users/alex" not in json.dumps(result_hit)


async def test_kb_batch_search_hits_include_source_ref(
    db_session,
    mcp_principal,
    mock_session_factory,
    monkeypatch,
) -> None:
    doc = _doc(title="Policy")
    await _add_watched_doc(db_session, doc, relative_path="policies/policy.pdf")
    hit = _search_hit(doc)

    async def fake_hybrid_search(*args, **kwargs):
        return SearchResult(hits=[hit], total_candidates=1)

    monkeypatch.setattr("harbor_clerk.mcp_server.hybrid_search", fake_hybrid_search)

    payload = json.loads(await kb_batch_search(["policy"]))
    result_hit = payload["results"][0]["hits"][0]

    assert result_hit["citation"] == "Policy, pp. 4-5"
    assert result_hit["source"]["relative_path"] == "policies/policy.pdf"
    assert "/Users/alex" not in json.dumps(result_hit)


async def test_kb_find_all_result_includes_source_ref(
    db_session,
    mcp_principal,
    mock_session_factory,
    monkeypatch,
) -> None:
    doc = _doc(title="Operations Manual")
    await _add_watched_doc(db_session, doc, relative_path="manuals/ops.pdf")

    async def fake_find_all(*args, **kwargs):
        return FindAllResult(
            hits=[
                FindAllHit(
                    doc_id=str(doc.doc_id),
                    doc_title="Operations Manual",
                    mime_type="application/pdf",
                    language="en",
                    score=0.88,
                    ingested_at=datetime(2026, 6, 3, tzinfo=UTC),
                    page_range="2",
                    top_chunk_id=str(uuid.uuid4()),
                    top_chunk_text="Use the lockout checklist before maintenance.",
                    top_chunk_page=2,
                    top_chunk_heading="Safety",
                )
            ],
            total_matches=1,
            offset=0,
            truncated=False,
            sort_by="relevance",
            presentation="full",
        )

    monkeypatch.setattr("harbor_clerk.mcp_server.find_all", fake_find_all)

    payload = json.loads(await kb_find_all("lockout", presentation="full"))
    result = payload["results"][0]

    assert result["citation"] == "Operations Manual, p. 2"
    assert result["source"]["section"] == "Safety"
    assert result["source"]["relative_path"] == "manuals/ops.pdf"
    assert result["top_chunk"]["heading"] == "Safety"
    assert "/Users/alex" not in json.dumps(result)


async def test_kb_read_passages_and_expand_context_include_source_refs(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    doc = _doc(title="Runbook")
    await _add_watched_doc(db_session, doc, relative_path="runbooks/main.pdf")
    chunks = []
    for i in range(3):
        chunk = Chunk(
            doc_id=doc.doc_id,
            chunk_num=i,
            chunk_text=f"Runbook chunk {i}",
            page_start=i + 1,
            page_end=i + 1,
            language="en",
            ocr_used=False,
        )
        db_session.add(chunk)
        chunks.append(chunk)
    await db_session.flush()

    read_payload = json.loads(await kb_read_passages([str(chunks[1].chunk_id)]))
    passage = read_payload["passages"][0]
    assert passage["doc_id"] == str(doc.doc_id)
    assert passage["citation"] == "Runbook, p. 2"
    assert passage["source"]["chunk_id"] == str(chunks[1].chunk_id)
    assert passage["source"]["relative_path"] == "runbooks/main.pdf"

    expand_payload = json.loads(await kb_expand_context(str(chunks[1].chunk_id), n=1))
    target = next(chunk for chunk in expand_payload["chunks"] if chunk.get("is_target"))
    assert target["citation"] == "Runbook, p. 2"
    assert target["source"]["chunk_id"] == str(chunks[1].chunk_id)
    assert target["source"]["relative_path"] == "runbooks/main.pdf"
    assert "/Users/alex" not in json.dumps(read_payload)
    assert "/Users/alex" not in json.dumps(expand_payload)


async def test_kb_get_document_includes_source_ref(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    doc = _doc(title="Board Packet")
    await _add_watched_doc(db_session, doc, relative_path="board/packet.pdf")

    payload = json.loads(await kb_get_document(str(doc.doc_id)))

    assert payload["citation"] == "Board Packet"
    assert payload["source"]["doc_id"] == str(doc.doc_id)
    assert payload["source"]["relative_path"] == "board/packet.pdf"
    assert "source_path" not in payload
    assert "/Users/alex" not in json.dumps(payload)


async def test_kb_list_recent_and_corpus_overview_document_rows_include_source_refs(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    doc = _doc(title="Recent Contract")
    await _add_watched_doc(db_session, doc, relative_path="recent/contract.pdf")

    recent = json.loads(await kb_list_recent(limit=5))
    recent_row = next(row for row in recent["documents"] if row["doc_id"] == str(doc.doc_id))
    assert recent_row["citation"] == "Recent Contract"
    assert recent_row["source"]["relative_path"] == "recent/contract.pdf"

    overview = json.loads(await kb_corpus_overview(limit=5))
    overview_row = next(row for row in overview["documents"] if row["doc_id"] == str(doc.doc_id))
    assert overview_row["citation"] == "Recent Contract"
    assert overview_row["source"]["relative_path"] == "recent/contract.pdf"
    assert "/Users/alex" not in json.dumps(overview_row)


async def test_kb_document_outline_includes_source_ref(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    doc = _doc(title="Manual")
    await _add_watched_doc(db_session, doc, relative_path="manuals/main.pdf")

    payload = json.loads(await kb_document_outline(str(doc.doc_id)))

    assert payload["citation"] == "Manual"
    assert payload["source"]["relative_path"] == "manuals/main.pdf"


async def test_kb_find_related_preserves_legacy_source_and_adds_source_ref(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    source_doc = _doc(title="Source")
    related_doc = _doc(title="Related", sha256=b"y" * 32)
    await _add_watched_doc(db_session, source_doc, relative_path="source.pdf")
    await _add_watched_doc(db_session, related_doc, relative_path="related.pdf")
    db_session.add(
        DocumentLink(
            src_doc_id=source_doc.doc_id,
            target_doc_id=related_doc.doc_id,
            link_text="Related",
            target_title="related",
            resolved=True,
        )
    )
    await db_session.flush()

    payload = json.loads(await kb_find_related(str(source_doc.doc_id)))
    related = payload["related"][0]

    assert related["source"] == "linked"
    assert related["citation"] == "Related"
    assert related["source_ref"]["doc_id"] == str(related_doc.doc_id)
    assert related["source_ref"]["relative_path"] == "related.pdf"


async def test_kb_verify_identifier_and_documents_by_date_rows_include_source_refs(
    db_session,
    mcp_principal,
    mock_session_factory,
) -> None:
    doc = _doc(
        title="Pinnacle Contract",
        canonical_filename="pinnacle.pdf",
        doc_metadata={"tika": {"created_at": "2024-01-01T00:00:00Z"}},
    )
    await _add_watched_doc(db_session, doc, relative_path="contracts/pinnacle.pdf")
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="California contract", language="english"))
    await db_session.flush()

    verified = json.loads(await kb_verify_identifier("Pinnacle Contract"))
    assert verified["status"] == "unique"
    assert verified["match"]["citation"] == "Pinnacle Contract"
    assert verified["match"]["source"]["relative_path"] == "contracts/pinnacle.pdf"

    by_date = json.loads(await kb_documents_by_date(direction="earliest", query="California", limit=5))
    row = next(row for row in by_date["results"] if row["doc_id"] == str(doc.doc_id))
    assert row["citation"] == "Pinnacle Contract"
    assert row["source"]["relative_path"] == "contracts/pinnacle.pdf"
    assert "/Users/alex" not in json.dumps(row)
