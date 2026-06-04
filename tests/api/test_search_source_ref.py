"""REST search and passage SourceRef contract tests."""

from uuid import uuid4

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.search_types import SearchHit, SearchResult
from tests.conftest import auth_header


def _doc(**kwargs) -> Document:
    defaults = {
        "title": "NDA",
        "canonical_filename": "nda.pdf",
        "status": "active",
        "sha256": b"x" * 32,
        "pipeline_status": PipelineStatus.ready,
        "source_path": "/Users/alex/private/contracts/client-a/nda.pdf",
        "doc_metadata": {},
    }
    defaults.update(kwargs)
    return Document(**defaults)


async def _add_watched_doc(db_session, *, doc: Document, relative_path: str = "client-a/nda.pdf") -> WatchedFolder:
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
    await db_session.commit()
    return folder


async def test_search_hit_includes_source_and_citation(client, admin_token, db_session, monkeypatch) -> None:
    doc = _doc()
    await _add_watched_doc(db_session, doc=doc)
    chunk_id = uuid4()

    async def fake_hybrid_search(*args, **kwargs):
        return SearchResult(
            hits=[
                SearchHit(
                    chunk_id=str(chunk_id),
                    doc_id=str(doc.doc_id),
                    chunk_num=0,
                    chunk_text="The NDA terminates on written notice.",
                    page_start=4,
                    page_end=5,
                    language="en",
                    ocr_used=False,
                    ocr_confidence=None,
                    score=0.91,
                    doc_title="NDA",
                )
            ],
            total_candidates=1,
            reranker_status="disabled",
        )

    monkeypatch.setattr("harbor_clerk.api.routes.search.hybrid_search", fake_hybrid_search)

    resp = await client.post(
        "/api/search",
        json={"query": "termination"},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 200, resp.text
    hit = resp.json()["hits"][0]
    assert hit["doc_id"] == str(doc.doc_id)
    assert hit["doc_title"] == "NDA"
    assert hit["page_start"] == 4
    assert hit["page_end"] == 5
    assert hit["citation"] == "NDA, pp. 4-5"
    assert hit["source"]["doc_id"] == str(doc.doc_id)
    assert hit["source"]["chunk_id"] == str(chunk_id)
    assert hit["source"]["source_kind"] == "document"
    assert hit["source"]["folder_label"] == "Contracts"
    assert hit["source"]["relative_path"] == "client-a/nda.pdf"
    assert hit["source"]["citation"] == "NDA, pp. 4-5"
    assert "/Users/alex" not in str(hit["source"])


async def test_faceted_search_hit_includes_source(client, admin_token, db_session, monkeypatch) -> None:
    doc = _doc(title="Policy")
    await _add_watched_doc(db_session, doc=doc, relative_path="policy.pdf")
    chunk_id = uuid4()

    async def fake_hybrid_search(*args, **kwargs):
        return SearchResult(
            hits=[
                SearchHit(
                    chunk_id=str(chunk_id),
                    doc_id=str(doc.doc_id),
                    chunk_num=0,
                    chunk_text="Policy excerpt",
                    page_start=1,
                    page_end=1,
                    language="en",
                    ocr_used=False,
                    ocr_confidence=None,
                    score=0.72,
                    doc_title="Policy",
                )
            ],
            total_candidates=1,
            reranker_status="disabled",
        )

    monkeypatch.setattr("harbor_clerk.api.routes.search.hybrid_search", fake_hybrid_search)

    resp = await client.post(
        "/api/search",
        json={"query": "policy", "faceted": True},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 200, resp.text
    hit = resp.json()["documents"][0]["hits"][0]
    assert hit["citation"] == "Policy, p. 1"
    assert hit["source"]["citation"] == "Policy, p. 1"


async def test_read_passages_includes_source_and_citation(client, admin_token, db_session) -> None:
    doc = _doc(title="Operations Manual", canonical_filename="ops.pdf")
    await _add_watched_doc(db_session, doc=doc, relative_path="manuals/ops.pdf")
    chunk = Chunk(
        doc_id=doc.doc_id,
        chunk_num=0,
        chunk_text="Use the lockout checklist before maintenance.",
        page_start=2,
        page_end=2,
        language="en",
        ocr_used=False,
    )
    db_session.add(chunk)
    await db_session.commit()

    resp = await client.post(
        "/api/passages/read",
        json={"chunk_ids": [str(chunk.chunk_id)]},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 200, resp.text
    passage = resp.json()["passages"][0]
    assert passage["doc_id"] == str(doc.doc_id)
    assert passage["citation"] == "Operations Manual, p. 2"
    assert passage["source"]["chunk_id"] == str(chunk.chunk_id)
    assert passage["source"]["relative_path"] == "manuals/ops.pdf"
    assert passage["source"]["citation"] == "Operations Manual, p. 2"
    assert "/Users/alex" not in str(passage["source"])


async def test_read_passages_attachment_source_uses_parent_email(client, admin_token, db_session) -> None:
    parent = _doc(
        title="Budget follow-up",
        canonical_filename="budget-follow-up.eml",
        mime_type="message/rfc822",
        email_from_name="Jane Doe",
        email_subject="Budget follow-up",
    )
    db_session.add(parent)
    await db_session.flush()

    attachment = _doc(
        title="invoice.pdf",
        canonical_filename="invoice.pdf",
        mime_type="application/pdf",
        email_parent_doc_id=parent.doc_id,
    )
    await _add_watched_doc(db_session, doc=attachment, relative_path="mail/attachments/invoice.pdf")
    chunk = Chunk(
        doc_id=attachment.doc_id,
        chunk_num=0,
        chunk_text="Invoice total is due on receipt.",
        page_start=2,
        page_end=2,
        language="en",
        ocr_used=False,
    )
    db_session.add(chunk)
    await db_session.commit()

    resp = await client.post(
        "/api/passages/read",
        json={"chunk_ids": [str(chunk.chunk_id)]},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 200, resp.text
    passage = resp.json()["passages"][0]
    assert passage["source"]["source_kind"] == "attachment"
    assert passage["citation"] == 'Attachment "invoice.pdf", p. 2, to Email from Jane Doe, "Budget follow-up"'
