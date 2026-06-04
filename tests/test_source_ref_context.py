from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.source_ref import load_source_ref_context


def _doc(**kwargs) -> Document:
    defaults = {
        "title": "Contract A",
        "canonical_filename": "contract-a.pdf",
        "status": "active",
        "sha256": b"x" * 32,
        "pipeline_status": PipelineStatus.ready,
        "doc_metadata": {},
    }
    defaults.update(kwargs)
    return Document(**defaults)


@pytest.mark.asyncio
async def test_context_loads_folder_label_and_relative_path(db_session) -> None:
    folder = WatchedFolder(path="/Users/alex/private/contracts", display_name="Contracts")
    db_session.add(folder)
    await db_session.flush()

    doc = _doc(title="NDA", source_path="/Users/alex/private/contracts/client-a/nda.pdf")
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path="client-a/nda.pdf",
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.flush()

    ctx = await load_source_ref_context(db_session, [doc.doc_id])
    ref = ctx.ref_for_doc(doc.doc_id)
    payload = ref.to_dict()

    assert payload["folder_label"] == "Contracts"
    assert payload["relative_path"] == "client-a/nda.pdf"
    assert payload["citation"] == "NDA"
    assert "/Users/alex" not in str(payload)


@pytest.mark.asyncio
async def test_context_loads_parent_email_for_attachment(db_session) -> None:
    parent = _doc(
        title="Budget follow-up",
        canonical_filename="budget-follow-up.eml",
        mime_type="message/rfc822",
        email_from_name="Jane Doe",
        email_subject="Budget follow-up",
        email_date_sent=datetime(2025, 3, 7, tzinfo=UTC),
    )
    db_session.add(parent)
    await db_session.flush()

    attachment = _doc(
        title="invoice.pdf",
        canonical_filename="invoice.pdf",
        mime_type="application/pdf",
        email_parent_doc_id=parent.doc_id,
    )
    db_session.add(attachment)
    await db_session.flush()

    ctx = await load_source_ref_context(db_session, [attachment.doc_id])
    ref = ctx.ref_for_doc(attachment.doc_id, pages="2")

    assert ref.source_kind == "attachment"
    assert ref.citation == 'Attachment "invoice.pdf", p. 2, to Email from Jane Doe, "Budget follow-up", Mar 7, 2025'


@pytest.mark.asyncio
async def test_context_handles_missing_watched_rows(db_session) -> None:
    doc = _doc(title="Loose Document")
    db_session.add(doc)
    await db_session.flush()

    ctx = await load_source_ref_context(db_session, [doc.doc_id])
    ref = ctx.ref_for_doc(doc.doc_id)
    payload = ref.to_dict()

    assert payload["citation"] == "Loose Document"
    assert "folder_label" not in payload
    assert "relative_path" not in payload


@pytest.mark.asyncio
async def test_context_soft_fails_watched_lookup_without_poisoning_session(db_session) -> None:
    doc = _doc(title="Loose Document")
    db_session.add(doc)
    await db_session.flush()

    original_name = WatchedFile.__table__.name
    WatchedFile.__table__.name = "watched_files_missing_for_source_ref_test"
    try:
        ctx = await load_source_ref_context(db_session, [doc.doc_id])
        ref = ctx.ref_for_doc(doc.doc_id)

        assert ref.citation == "Loose Document"
        # The optional watched-folder lookup failed, but the caller's session
        # must remain usable for later route/tool queries.
        loaded_doc_id = await db_session.scalar(select(Document.doc_id).where(Document.doc_id == doc.doc_id))
        assert loaded_doc_id == doc.doc_id
    finally:
        WatchedFile.__table__.name = original_name


@pytest.mark.asyncio
async def test_context_accepts_string_doc_ids(db_session) -> None:
    doc = _doc(title="String id")
    db_session.add(doc)
    await db_session.flush()

    ctx = await load_source_ref_context(db_session, [str(doc.doc_id)])
    ref = ctx.ref_for_doc(str(doc.doc_id), chunk_id=uuid4(), pages="1")

    assert ref.doc_id == str(doc.doc_id)
    assert ref.citation == "String id, p. 1"
