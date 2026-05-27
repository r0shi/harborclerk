"""Integration tests for hybrid_search() with filtering parameters."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_docs(db_session: AsyncSession):
    """Create 2 documents with different languages, MIME types, and dates.

    doc_a: PDF, english chunks, created "2025-01-15"
    doc_b: text/plain, french chunks, created "2025-06-15"
    """
    # --- Document A ---
    doc_a = Document(
        title="Report Alpha",
        status="active",
        sha256=b"sha256_alpha_aaaaaaaaaaaaaaaa\x00\x00\x00",
        mime_type="application/pdf",
        size_bytes=10000,
        pipeline_status=PipelineStatus.ready,
        source_path="alpha.pdf",
    )
    db_session.add(doc_a)
    await db_session.flush()
    # Backdate created_at
    doc_a.created_at = datetime(2025, 1, 15, tzinfo=UTC)
    await db_session.flush()

    for i in range(3):
        db_session.add(
            Chunk(
                doc_id=doc_a.doc_id,
                chunk_num=i,
                page_start=i + 1,
                page_end=i + 1,
                char_start=i * 500,
                char_end=(i + 1) * 500,
                chunk_text=f"Alpha report section {i} about quarterly results.",
                language="english",
            )
        )
    await db_session.flush()

    # --- Document B ---
    doc_b = Document(
        title="Rapport Beta",
        status="active",
        sha256=b"sha256_beta_bbbbbbbbbbbbbbbbb\x00\x00\x00",
        mime_type="text/plain",
        size_bytes=5000,
        pipeline_status=PipelineStatus.ready,
        source_path="beta.txt",
    )
    db_session.add(doc_b)
    await db_session.flush()
    doc_b.created_at = datetime(2025, 6, 15, tzinfo=UTC)
    await db_session.flush()

    for i in range(3):
        db_session.add(
            Chunk(
                doc_id=doc_b.doc_id,
                chunk_num=i,
                page_start=i + 1,
                page_end=i + 1,
                char_start=i * 500,
                char_end=(i + 1) * 500,
                chunk_text=f"Beta rapport section {i} sur les résultats trimestriels.",
                language="french",
            )
        )
    await db_session.flush()

    return doc_a, doc_b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_hybrid_search_multi_doc_filter(db_session, two_docs):
    """doc_ids filter returns only chunks from specified documents."""
    doc_a, doc_b = two_docs
    result = await hybrid_search(
        db_session,
        "section results",
        k=10,
        doc_ids=[doc_a.doc_id],
    )
    for h in result.hits:
        assert h.doc_id == str(doc_a.doc_id)


async def test_hybrid_search_language_filter(db_session, two_docs):
    """Language filter returns only matching-language chunks."""
    doc_a, doc_b = two_docs
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        language="french",
    )
    for h in result.hits:
        assert h.language == "french"
        assert h.doc_id == str(doc_b.doc_id)


async def test_hybrid_search_date_range_filter(db_session, two_docs):
    """after/before filters restrict by document created_at."""
    doc_a, doc_b = two_docs

    # Only documents after 2025-03-01 → should get doc_b only
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        after=datetime(2025, 3, 1, tzinfo=UTC),
    )
    for h in result.hits:
        assert h.doc_id == str(doc_b.doc_id)

    # Only documents before 2025-03-01 → should get doc_a only
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        before=datetime(2025, 3, 1, tzinfo=UTC),
    )
    for h in result.hits:
        assert h.doc_id == str(doc_a.doc_id)


async def test_hybrid_search_mime_type_filter(db_session, two_docs):
    """mime_type filter returns only chunks from matching documents."""
    doc_a, doc_b = two_docs
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        mime_type="text/plain",
    )
    for h in result.hits:
        assert h.doc_id == str(doc_b.doc_id)


async def test_hybrid_search_combined_filters(db_session, two_docs):
    """Multiple filters combine (AND) correctly."""
    doc_a, doc_b = two_docs

    # language=french AND mime_type=application/pdf → no results
    result = await hybrid_search(
        db_session,
        "section",
        k=10,
        language="french",
        mime_type="application/pdf",
    )
    assert len(result.hits) == 0


async def test_hybrid_search_doc_id_doc_ids_raises(db_session, two_docs):
    """Providing both doc_id and doc_ids raises ValueError."""
    doc_a, doc_b = two_docs
    with pytest.raises(ValueError, match="both doc_id and doc_ids"):
        await hybrid_search(
            db_session,
            "section",
            k=10,
            doc_id=doc_a.doc_id,
            doc_ids=[doc_b.doc_id],
        )


async def test_hybrid_search_text_contains_filters_chunks(db_session):
    """text_contains restricts candidates to chunks whose text contains
    the substring (case-insensitive). Match the spec wording: 'a document
    is a candidate iff at least one of its chunks contains the substring'."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    # Doc A contains "off-balance-sheet" exactly
    doc_a = Document(
        title="A", status="active", sha256=b"sha_a_0000000000000000000000000", pipeline_status=PipelineStatus.ready
    )
    db_session.add(doc_a)
    await db_session.flush()
    db_session.add(
        Chunk(doc_id=doc_a.doc_id, chunk_num=0, chunk_text="The off-balance-sheet entity was disclosed.", language="en")
    )

    # Doc B is semantically similar but lacks the literal phrase
    doc_b = Document(
        title="B", status="active", sha256=b"sha_b_0000000000000000000000000", pipeline_status=PipelineStatus.ready
    )
    db_session.add(doc_b)
    await db_session.flush()
    db_session.add(
        Chunk(
            doc_id=doc_b.doc_id, chunk_num=0, chunk_text="The unconsolidated subsidiary was disclosed.", language="en"
        )
    )
    await db_session.flush()

    # No text_contains: both candidates eligible (query matches both docs)
    result = await hybrid_search(db_session, "disclosed", k=10)
    assert {h.doc_id for h in result.hits} == {str(doc_a.doc_id), str(doc_b.doc_id)}

    # With text_contains="off-balance-sheet": only Doc A
    result = await hybrid_search(
        db_session,
        "disclosed",
        k=10,
        text_contains="off-balance-sheet",
    )
    assert {h.doc_id for h in result.hits} == {str(doc_a.doc_id)}


async def test_hybrid_search_text_contains_case_insensitive(db_session):
    """Case differences in the substring match."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    doc = Document(
        title="A", status="active", sha256=b"sha_x_0000000000000000000000000", pipeline_status=PipelineStatus.ready
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="The OFF-BALANCE-SHEET entity.", language="en"))
    await db_session.flush()

    result = await hybrid_search(
        db_session,
        "entity",
        k=10,
        text_contains="off-balance-sheet",
    )
    assert result.hits and result.hits[0].doc_id == str(doc.doc_id)


async def test_hybrid_search_text_contains_escapes_special_chars(db_session):
    """% and _ in the substring are matched literally, not as wildcards."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    doc1 = Document(
        title="A", status="active", sha256=b"sha_p_0000000000000000000000000", pipeline_status=PipelineStatus.ready
    )
    db_session.add(doc1)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc1.doc_id, chunk_num=0, chunk_text="Revenue grew 50% YoY.", language="en"))

    doc2 = Document(
        title="B", status="active", sha256=b"sha_q_0000000000000000000000000", pipeline_status=PipelineStatus.ready
    )
    db_session.add(doc2)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc2.doc_id, chunk_num=0, chunk_text="Revenue grew significantly.", language="en"))
    await db_session.flush()

    # "50%" should match only Doc 1; the % must NOT act as a wildcard
    result = await hybrid_search(
        db_session,
        "revenue",
        k=10,
        text_contains="50%",
    )
    assert {h.doc_id for h in result.hits} == {str(doc1.doc_id)}


async def test_metadata_filter_email_from_address_exact(db_session):
    """email.from_address matches case-insensitively."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_ef_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_address="alice@firm.com",
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_ef_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_address="bob@firm.com",
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="quarterly report", language="en"))
    await db_session.flush()

    # Exact match
    res = await hybrid_search(
        db_session,
        "quarterly",
        k=10,
        metadata_filter={"email.from_address": "alice@firm.com"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}

    # Case-insensitive
    res2 = await hybrid_search(
        db_session,
        "quarterly",
        k=10,
        metadata_filter={"email.from_address": "ALICE@FIRM.COM"},
    )
    assert {h.doc_id for h in res2.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_from_name_contains(db_session):
    """email.from_name_contains does ILIKE substring (case-insensitive)."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_fc_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_name="Alice Anderson",
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_fc_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_name="Bob Smith",
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="quarterly report", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "quarterly",
        k=10,
        metadata_filter={"email.from_name_contains": "alice"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_subject_contains(db_session):
    """email.subject_contains does ILIKE substring (case-insensitive)."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_sc_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_subject="Q3 Vendor Agreement Review",
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_sc_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_subject="Holiday party planning",
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.subject_contains": "VENDOR"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_to_addresses_array_element(db_session):
    """email.to_addresses matches when the address appears in the array."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_ta_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["bob@firm.com", "carol@firm.com"],
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_ta_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["dan@firm.com"],
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.to_addresses": "carol@firm.com"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_to_addresses_list_any(db_session):
    """List value matches when ANY element appears in the array."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_tl_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["bob@firm.com", "carol@firm.com"],
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_tl_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["zelda@elsewhere.com"],
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.to_addresses": ["carol@firm.com", "nobody@nope.com"]},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_contains_escapes_special_chars(db_session):
    """ILIKE special chars (% _) in input are matched literally."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_se_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_subject="Revenue grew 50% YoY",
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_se_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_subject="Revenue grew 50X YoY",
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    # "50%" must match only Doc A; '%' must NOT act as a wildcard
    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.subject_contains": "50%"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_unknown_key_raises(db_session):
    """Unknown email.* key raises ValueError (loud failure)."""
    import pytest

    from harbor_clerk.search import hybrid_search

    with pytest.raises(ValueError, match="email"):
        await hybrid_search(
            db_session,
            "content",
            k=10,
            metadata_filter={"email.bogus_field": "x"},
        )


async def test_metadata_filter_email_cc_addresses_array_element(db_session):
    """email.cc_addresses matches when the address appears in the cc array."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_ca_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_cc_addresses=["bob@firm.com", "carol@firm.com"],
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_ca_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_cc_addresses=["dan@firm.com"],
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.cc_addresses": "carol@firm.com"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_array_empty_list_matches_nothing(db_session):
    """Empty list value matches no documents (not all, which is the bug
    caught by code review)."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_el_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["bob@firm.com"],
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_el_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_to_addresses=["carol@firm.com"],
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    # Empty list to_addresses should match nothing
    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.to_addresses": []},
    )
    assert res.hits == []

    # Same for cc_addresses
    res2 = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={"email.cc_addresses": []},
    )
    assert res2.hits == []


async def test_metadata_filter_email_does_not_break_jsonb(db_session):
    """Existing JSONB metadata_filter still works alongside email.* keys."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(
        title="A",
        status="active",
        sha256=b"sha_jx_a_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_address="alice@firm.com",
        doc_metadata={"sidecar": {"vendor": "Acme"}},
    )
    b = Document(
        title="B",
        status="active",
        sha256=b"sha_jx_b_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        email_from_address="alice@firm.com",
        doc_metadata={"sidecar": {"vendor": "Globex"}},
    )
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="content", language="en"))
    await db_session.flush()

    # Both filters apply (AND): from Alice AND vendor=Acme
    res = await hybrid_search(
        db_session,
        "content",
        k=10,
        metadata_filter={
            "email.from_address": "alice@firm.com",
            "sidecar.vendor": "Acme",
        },
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}
