"""kb_search metadata_filter — JSONB @> containment + ? existence fallback."""

import uuid

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search


async def _seed_doc(db_session, *, title: str, metadata: dict) -> Document:
    """Helper: insert a Document with one trivial Chunk so FTS can match."""
    doc = Document(
        title=title,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata,
    )
    db_session.add(doc)
    await db_session.flush()
    chunk = Chunk(
        doc_id=doc.doc_id,
        chunk_num=0,
        chunk_text="jurisdiction indemnification arbitration clause",
        language="english",
    )
    db_session.add(chunk)
    await db_session.flush()
    return doc


async def test_metadata_filter_pins_doc_by_scalar_field(db_session):
    """A scalar filter on a scalar metadata value matches via @> containment."""
    pinnacle = await _seed_doc(
        db_session,
        title="vendor-pinnacle-A",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    other = await _seed_doc(
        db_session,
        title="vendor-other",
        metadata={"sidecar": {"vendor": "Other Vendor, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions, LLC"},
    )
    cited_doc_ids = {hit.doc_id for hit in result.hits}
    assert str(pinnacle.doc_id) in cited_doc_ids
    assert str(other.doc_id) not in cited_doc_ids


async def test_metadata_filter_pins_doc_by_list_value_via_existence(db_session):
    """A scalar filter on a list-valued metadata field matches if the list
    contains the scalar (JSONB ? operator)."""
    tagged = await _seed_doc(
        db_session,
        title="tagged-alpha",
        metadata={"frontmatter": {"tags": ["alpha", "beta"]}},
    )
    untagged = await _seed_doc(
        db_session,
        title="untagged",
        metadata={"frontmatter": {"tags": ["gamma"]}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"frontmatter.tags": "alpha"},
    )
    cited = {hit.doc_id for hit in result.hits}
    assert str(tagged.doc_id) in cited
    assert str(untagged.doc_id) not in cited


async def test_metadata_filter_combines_multiple_keys_with_and(db_session):
    """Two filter keys → AND. Both must match for a doc to be returned."""
    a = await _seed_doc(
        db_session,
        title="vendor-pinnacle-24mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    b = await _seed_doc(
        db_session,
        title="vendor-pinnacle-12mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={
            "sidecar.vendor": "Pinnacle Tech Solutions, LLC",
            "sidecar.term_months": 24,
        },
    )
    cited = {hit.doc_id for hit in result.hits}
    assert str(a.doc_id) in cited
    assert str(b.doc_id) not in cited


async def test_metadata_filter_empty_match_returns_no_hits(db_session):
    """A filter that matches no docs returns an empty result set (not an error)."""
    await _seed_doc(
        db_session,
        title="vendor-pinnacle",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC"}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"sidecar.vendor": "Nonexistent Vendor Co"},
    )
    assert result.hits == []


async def test_metadata_filter_absent_falls_back_to_existing_behavior(db_session):
    """When metadata_filter is None, behavior is unchanged."""
    doc = await _seed_doc(db_session, title="any", metadata={})
    await db_session.commit()
    result = await hybrid_search(db_session, query="jurisdiction arbitration", k=10, metadata_filter=None)
    assert any(hit.doc_id == str(doc.doc_id) for hit in result.hits)
