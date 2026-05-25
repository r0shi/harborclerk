"""Algorithm + integration tests for src/harbor_clerk/mcp_lookup_tools.py.

Task 2 (this file) covers candidate matching for verify_identifier:
title + canonical_filename ILIKE; metadata.tika.title equals; identifier-like
metadata key equals across sidecar/frontmatter.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mcp_lookup_tools import _find_candidates, verify_identifier
from harbor_clerk.models import Document
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
