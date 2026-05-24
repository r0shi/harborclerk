"""Tests for the DocumentLink model and the 0002 migration."""

import uuid

import pytest
from sqlalchemy import select

from harbor_clerk.models import Document, DocumentLink
from harbor_clerk.models.enums import PipelineStatus


@pytest.fixture
def sync_session(_engine):
    """Per-test sync session with table cleanup (mirrors db_session's async cleanup)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from harbor_clerk.models import Base

    sync_url = str(_engine.url).replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url, echo=False)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        # Cleanup all tables (sync) — mirrors db_session's async cleanup
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name == "schema_metadata":
                    continue
                conn.execute(table.delete())
        engine.dispose()


def test_document_link_model_importable():
    """DocumentLink must be importable from harbor_clerk.models."""
    assert DocumentLink.__tablename__ == "document_links"


def test_document_link_required_fields(sync_session):
    """A minimal DocumentLink can be inserted with just src_doc_id + link_text + target_title."""
    doc = Document(
        title="src note",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add(doc)
    sync_session.flush()

    link = DocumentLink(
        src_doc_id=doc.doc_id,
        link_text="Target Note",
        target_title="target note",
    )
    sync_session.add(link)
    sync_session.commit()

    row = sync_session.execute(select(DocumentLink).where(DocumentLink.src_doc_id == doc.doc_id)).scalar_one()
    assert row.link_text == "Target Note"
    assert row.target_title == "target note"
    assert row.target_doc_id is None
    assert row.anchor is None
    assert row.alias is None
    assert row.resolved is False
    assert row.created_at is not None
    assert isinstance(row.link_id, uuid.UUID)


def test_document_link_optional_fields(sync_session):
    """anchor, alias, target_doc_id, resolved can all be populated."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    tgt = Document(
        title="tgt",
        status="active",
        sha256=b"\x02" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add_all([src, tgt])
    sync_session.flush()

    link = DocumentLink(
        src_doc_id=src.doc_id,
        target_doc_id=tgt.doc_id,
        link_text="Target Note#Section|Alias",
        target_title="target note",
        anchor="Section",
        alias="Alias",
        resolved=True,
    )
    sync_session.add(link)
    sync_session.commit()

    row = sync_session.execute(select(DocumentLink).where(DocumentLink.link_id == link.link_id)).scalar_one()
    assert row.target_doc_id == tgt.doc_id
    assert row.anchor == "Section"
    assert row.alias == "Alias"
    assert row.resolved is True


def test_document_link_src_cascade_deletes_links(sync_session):
    """Deleting the source document cascades to its outgoing links."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x03" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add(src)
    sync_session.flush()
    sync_session.add(DocumentLink(src_doc_id=src.doc_id, link_text="x", target_title="x"))
    sync_session.commit()

    sync_session.delete(src)
    sync_session.commit()

    rows = sync_session.execute(select(DocumentLink)).scalars().all()
    assert rows == []


def test_document_link_target_set_null_on_target_delete(sync_session):
    """Deleting the target document sets target_doc_id NULL on incoming links
    (NOT CASCADE — links remain so the graph remembers the broken reference)."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x04" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    tgt = Document(
        title="tgt",
        status="active",
        sha256=b"\x05" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add_all([src, tgt])
    sync_session.flush()
    sync_session.add(
        DocumentLink(
            src_doc_id=src.doc_id,
            target_doc_id=tgt.doc_id,
            link_text="tgt",
            target_title="tgt",
            resolved=True,
        )
    )
    sync_session.commit()

    sync_session.delete(tgt)
    sync_session.commit()

    row = sync_session.execute(select(DocumentLink)).scalar_one()
    assert row.target_doc_id is None
    # `resolved` stays True historically — the resolver doesn't undo itself.
    # Re-resolution happens only when a new doc finalizes that matches.
    assert row.resolved is True  # SET NULL nulls only the FK, not the flag
