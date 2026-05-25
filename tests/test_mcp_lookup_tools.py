"""Algorithm + integration tests for src/harbor_clerk/mcp_lookup_tools.py.

Task 2 (this file) covers candidate matching for verify_identifier:
title + canonical_filename ILIKE; metadata.tika.title equals; identifier-like
metadata key equals across sidecar/frontmatter.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mcp_lookup_tools import _find_candidates, _query_documents_by_date, verify_identifier
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_doc(
    db_session: AsyncSession,
    *,
    title: str,
    canonical_filename: str | None = None,
    metadata: dict | None = None,
) -> Document:
    doc = Document(
        title=title,
        canonical_filename=canonical_filename,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata or {},
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# Matching tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_matches_returns_empty_list(db_session):
    await _seed_doc(db_session, title="Unrelated Doc")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "nothing-matches")
    assert candidates == []


@pytest.mark.asyncio
async def test_matches_by_title_contains(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await _seed_doc(db_session, title="Unrelated")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_canonical_filename_contains(db_session):
    target = await _seed_doc(db_session, title="Doc One", canonical_filename="0131_vendor_contract.pdf")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "vendor_contract")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_are_case_insensitive(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "PINNACLE")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_whitespace_normalized_in_input(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "  pinnacle   vendor  ")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_tika_title_equals(db_session):
    target = await _seed_doc(
        db_session,
        title="raw-filename",
        metadata={"tika": {"title": "Pinnacle Vendor Contract"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle Vendor Contract")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_sidecar_identifier_key_equals(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"sidecar": {"contract_id": "K-2025-031", "vendor": "Acme"}},
    )
    # Negative: another doc has the same value in a non-id key — must NOT match.
    await _seed_doc(
        db_session,
        title="Other doc",
        metadata={"sidecar": {"vendor": "K-2025-031"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2025-031")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_nested_identifier_key(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"sidecar": {"contract": {"id": "K-2025-031"}}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2025-031")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_list_valued_identifier_key(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"frontmatter": {"order_id": ["K-1", "K-2"]}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_deduplicates_when_multiple_fields_match(db_session):
    target = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract",
        canonical_filename="pinnacle-vendor-contract.pdf",
        metadata={"tika": {"title": "Pinnacle Vendor Contract"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle Vendor Contract")
    # All three columns match this doc; result must contain it once.
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_excludes_inactive_documents(db_session):
    doc = await _seed_doc(db_session, title="Pinnacle")
    doc.status = "deleted"
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert candidates == []


@pytest.mark.asyncio
async def test_caps_at_100_candidates(db_session):
    """100 docs with a shared substring — result is capped at 100."""
    for i in range(105):
        await _seed_doc(db_session, title=f"Pinnacle {i:03d}")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert len(candidates) == 100


# ---------------------------------------------------------------------------
# verify_identifier response-shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_returns_status_not_found(db_session):
    await _seed_doc(db_session, title="Unrelated")
    await db_session.flush()

    result = await verify_identifier(db_session, "nothing-matches")
    assert result == {"status": "not_found", "identifier": "nothing-matches"}


@pytest.mark.asyncio
async def test_unique_match_returns_status_unique(db_session):
    target = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract",
        canonical_filename="0131_vendor_contract.pdf",
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Vendor Contract")
    assert result["status"] == "unique"
    assert result["match"]["doc_id"] == str(target.doc_id)
    assert result["match"]["title"] == "Pinnacle Vendor Contract"
    assert result["match"]["canonical_filename"] == "0131_vendor_contract.pdf"
    assert result["match"]["discriminating_fields"] == {}


@pytest.mark.asyncio
async def test_ambiguous_match_with_discriminating_fields(db_session):
    a = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract A",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions", "term_months": 24}},
    )
    b = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract B",
        metadata={"sidecar": {"vendor": "Pinnacle Industries", "term_months": 36}},
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Vendor Contract")
    assert result["status"] == "ambiguous"
    assert result["count"] == 2
    ids = {c["doc_id"] for c in result["candidates"]}
    assert ids == {str(a.doc_id), str(b.doc_id)}

    # Per-candidate discriminating_fields should carry that candidate's own values.
    for c in result["candidates"]:
        assert "sidecar.vendor" in c["discriminating_fields"]
        assert "sidecar.term_months" in c["discriminating_fields"]
    suggestion = result["suggestion"]
    assert "sidecar.vendor" in suggestion or "sidecar.term_months" in suggestion


@pytest.mark.asyncio
async def test_ambiguous_with_no_differing_fields_uses_fallback_suggestion(db_session):
    await _seed_doc(
        db_session,
        title="Pinnacle Contract Alpha",
        metadata={"sidecar": {"vendor": "Same Vendor"}},
    )
    await _seed_doc(
        db_session,
        title="Pinnacle Contract Beta",
        metadata={"sidecar": {"vendor": "Same Vendor"}},
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Contract")
    assert result["status"] == "ambiguous"
    for c in result["candidates"]:
        assert c["discriminating_fields"] == {}
    assert "identical" in result["suggestion"].lower()


@pytest.mark.asyncio
async def test_empty_identifier_returns_error(db_session):
    result = await verify_identifier(db_session, "")
    assert "error" in result
    assert "non-empty" in result["error"]


@pytest.mark.asyncio
async def test_whitespace_only_identifier_returns_error(db_session):
    result = await verify_identifier(db_session, "   ")
    assert "error" in result


@pytest.mark.asyncio
async def test_overflow_flag_set_when_cap_exceeded(db_session):
    for i in range(105):
        await _seed_doc(db_session, title=f"Pinnacle {i:03d}")
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle")
    assert result["status"] == "ambiguous"
    assert result["count"] == 100
    assert result["overflow"] is True
    assert "More than 100" in result["suggestion"]


# ---------------------------------------------------------------------------
# _seed_doc_with_chunk helper
# ---------------------------------------------------------------------------


async def _seed_doc_with_chunk(
    db_session: AsyncSession,
    *,
    title: str,
    metadata: dict | None = None,
    text: str = "body text",
) -> Document:
    """Like _seed_doc but also adds a chunk so FTS queries can match."""
    doc = await _seed_doc(db_session, title=title, metadata=metadata or {})
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="english"))
    await db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# _query_documents_by_date tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earliest_orders_by_tika_date_ascending(db_session):
    older = await _seed_doc_with_chunk(
        db_session,
        title="Older email",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
    )
    newer = await _seed_doc_with_chunk(
        db_session,
        title="Newer email",
        metadata={"tika": {"created_at": "2001-08-14T08:34:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=10)
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    # Older first when direction=earliest. Other test fixtures may add docs
    # too (autouse cleanup), so only assert the relative order of these two.
    assert doc_ids.index(str(older.doc_id)) < doc_ids.index(str(newer.doc_id))


@pytest.mark.asyncio
async def test_latest_orders_by_date_descending(db_session):
    older = await _seed_doc_with_chunk(
        db_session,
        title="Older",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    newer = await _seed_doc_with_chunk(
        db_session,
        title="Newer",
        metadata={"tika": {"created_at": "2024-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="latest", limit=10)
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    assert doc_ids.index(str(newer.doc_id)) < doc_ids.index(str(older.doc_id))


@pytest.mark.asyncio
async def test_query_filters_to_fts_matching_docs(db_session):
    cali = await _seed_doc_with_chunk(
        db_session,
        title="California email",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
        text="discussion about California regulators",
    )
    await _seed_doc_with_chunk(
        db_session,
        title="Unrelated",
        metadata={"tika": {"created_at": "1999-10-14T00:00:00Z"}},
        text="discussion about something else entirely",
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", query="California", limit=10)
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(cali.doc_id) in doc_ids
    assert len(doc_ids) == 1


@pytest.mark.asyncio
async def test_metadata_filter_applied(db_session):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Pinnacle contract",
        metadata={
            "tika": {"created_at": "2024-01-01T00:00:00Z"},
            "sidecar": {"vendor": "Pinnacle Tech Solutions"},
        },
    )
    await _seed_doc_with_chunk(
        db_session,
        title="Other contract",
        metadata={
            "tika": {"created_at": "2024-01-01T00:00:00Z"},
            "sidecar": {"vendor": "Other Vendor"},
        },
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions"},
        limit=10,
    )
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    assert doc_ids == [str(target.doc_id)]


@pytest.mark.asyncio
async def test_after_filter_bounds_results(db_session):
    early = await _seed_doc_with_chunk(
        db_session,
        title="Early",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    late = await _seed_doc_with_chunk(
        db_session,
        title="Late",
        metadata={"tika": {"created_at": "2025-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", after="2024-01-01", limit=10)
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(late.doc_id) in doc_ids
    assert str(early.doc_id) not in doc_ids


@pytest.mark.asyncio
async def test_before_filter_bounds_results(db_session):
    early = await _seed_doc_with_chunk(
        db_session,
        title="Early",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    late = await _seed_doc_with_chunk(
        db_session,
        title="Late",
        metadata={"tika": {"created_at": "2025-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="latest", before="2024-01-01", limit=10)
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(early.doc_id) in doc_ids
    assert str(late.doc_id) not in doc_ids


@pytest.mark.asyncio
async def test_date_source_label_reflects_priority_chain(db_session):
    tika_doc = await _seed_doc_with_chunk(
        db_session,
        title="Tika source",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    fm_doc = await _seed_doc_with_chunk(
        db_session,
        title="FM source",
        metadata={"frontmatter": {"date": "2020-02-01"}},
    )
    sc_doc = await _seed_doc_with_chunk(
        db_session,
        title="Sidecar source",
        metadata={"sidecar": {"date": "2020-03-01"}},
    )
    ingest_only = await _seed_doc_with_chunk(db_session, title="Ingest only")
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=20)
    source_by_id = {str(d.doc_id): src for d, _, src in rows}
    assert source_by_id[str(tika_doc.doc_id)] == "tika.created_at"
    assert source_by_id[str(fm_doc.doc_id)] == "frontmatter.date"
    assert source_by_id[str(sc_doc.doc_id)] == "sidecar.date"
    assert source_by_id[str(ingest_only.doc_id)] == "ingest"


@pytest.mark.asyncio
async def test_explicit_date_field_skips_fallback(db_session):
    """date_field='sidecar.date' means docs without sidecar.date are sorted by NULL
    (PG sorts NULLs LAST asc / FIRST desc), not by Tika even if Tika exists."""
    sc_only = await _seed_doc_with_chunk(
        db_session,
        title="Sidecar only",
        metadata={"sidecar": {"date": "2020-01-01"}},
    )
    tika_only = await _seed_doc_with_chunk(
        db_session,
        title="Tika only",
        metadata={"tika": {"created_at": "2010-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", date_field="sidecar.date", limit=10)
    # sc_only is the first non-NULL date when sorting by sidecar.date asc;
    # tika_only is NULL on that field.
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    sc_idx = doc_ids.index(str(sc_only.doc_id))
    tika_idx = doc_ids.index(str(tika_only.doc_id))
    assert sc_idx < tika_idx
    # date_source label should reflect the explicit choice for the matching doc.
    source_by_id = {str(d.doc_id): src for d, _, src in rows}
    assert source_by_id[str(sc_only.doc_id)] == "sidecar.date"


@pytest.mark.asyncio
async def test_limit_respected(db_session):
    for i in range(5):
        await _seed_doc_with_chunk(
            db_session,
            title=f"Doc {i}",
            metadata={"tika": {"created_at": f"2024-01-{i + 1:02d}T00:00:00Z"}},
        )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_by_date_excludes_inactive_documents(db_session):
    doc = await _seed_doc_with_chunk(
        db_session,
        title="Will be deleted",
        metadata={"tika": {"created_at": "2024-01-01T00:00:00Z"}},
    )
    doc.status = "deleted"
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=10)
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(doc.doc_id) not in doc_ids


@pytest.mark.asyncio
async def test_malformed_tika_date_skipped_not_crash(db_session):
    """Doc with a non-ISO Tika date should fall through to next source,
    not abort the query with a 500."""
    bad = await _seed_doc_with_chunk(
        db_session,
        title="Bad date doc",
        metadata={
            "tika": {"created_at": "N/A"},
            "sidecar": {"date": "2024-01-01"},
        },
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=10)
    source_by_id = {str(d.doc_id): src for d, _, src in rows}
    # Tika date couldn't parse → fell through to sidecar.
    assert source_by_id[str(bad.doc_id)] == "sidecar.date"


@pytest.mark.asyncio
async def test_metadata_filter_list_value_match(db_session):
    """metadata_filter on a list-valued metadata field must match (parity with search.py)."""
    target = await _seed_doc_with_chunk(
        db_session,
        title="Tagged doc",
        metadata={
            "tika": {"created_at": "2024-01-01T00:00:00Z"},
            "frontmatter": {"tags": ["alpha", "beta"]},
        },
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"frontmatter.tags": "alpha"},
        limit=10,
    )
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(target.doc_id) in doc_ids
