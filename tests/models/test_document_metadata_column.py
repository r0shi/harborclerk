"""Tests for the doc_metadata JSONB column on Document."""

from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus


async def test_document_doc_metadata_defaults_to_empty_dict(db_session):
    """A freshly-created Document has doc_metadata = {}."""
    doc = Document(
        title="t",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    assert doc.doc_metadata == {}


async def test_document_doc_metadata_persists_arbitrary_dict(db_session):
    """The column stores and round-trips an arbitrary nested dict."""
    payload = {
        "tika": {"author": "Jane", "page_count": 3},
        "frontmatter": {"tags": ["alpha", "beta"]},
        "_source_provenance": {"tika": "2026-05-24T00:00:00+00:00"},
    }
    doc = Document(
        title="t",
        status="active",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
        doc_metadata=payload,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    assert doc.doc_metadata == payload
