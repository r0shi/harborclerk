"""Algorithm + integration tests for src/harbor_clerk/mcp_lookup_tools.py.

Task 2 (this file) covers candidate matching for verify_identifier:
title + canonical_filename ILIKE; metadata.tika.title equals; identifier-like
metadata key equals across sidecar/frontmatter.
"""

import json
import uuid
from contextlib import asynccontextmanager, contextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_lookup_tools import (
    _IDENTIFIER_KEY_RE,
    _find_candidates,
    _find_candidates_by_words,
    _normalize,
    _query_documents_by_date,
    documents_by_date,
    verify_identifier,
)
from harbor_clerk.mcp_server import (
    _mcp_principal,
    kb_documents_by_date,
    kb_verify_identifier,
)
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
async def test_metadata_pass_finds_a_match_behind_any_number_of_metadata_bearing_documents(db_session):
    """#763: the metadata pass read an unordered LIMIT 100 of the documents with any metadata, then matched
    among those. 150 documents with Tika and sidecar metadata but no matching identifier, inserted first,
    filled that window, and the one matching document (inserted last, titled to sort last) was never read.
    The pass now matches in SQL with no row limit, so it is found, and only it."""
    for i in range(150):
        await _seed_doc(
            db_session,
            title=f"NoMatch {i:03d}",
            metadata={"tika": {"title": f"Scan {i}"}, "sidecar": {"vendor": f"Vendor {i}", "contract_id": f"K-{i}"}},
        )
    matching = await _seed_doc(
        db_session,
        title="zzz Unique Title XZQ",
        metadata={"sidecar": {"contract_id": "UNIQUE-XZQ-9999"}},
    )
    await db_session.flush()

    for _ in range(3):
        candidates = await _find_candidates(db_session, "UNIQUE-XZQ-9999")
        assert [c.doc_id for c in candidates] == [matching.doc_id]


def _reference_leaves(metadata: dict) -> list:
    """The Python walk the metadata pass used before #763, kept here as the reference the SQL walk must
    agree with: objects descended whatever their key; under an identifier-like key a value, or each element
    of a list; only sidecar and frontmatter, and only when they are objects."""
    out: list = []

    def _walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, dict):
                    _walk(value)
                elif _IDENTIFIER_KEY_RE.match(str(key)):
                    if isinstance(value, list):
                        out.extend(value)
                    else:
                        out.append(value)

    for ns in ("sidecar", "frontmatter"):
        if isinstance(metadata.get(ns), dict):
            _walk(metadata[ns])
    return out


def _reference_match(metadata: dict, identifier: str, cap: int | None) -> bool:
    normalized = _normalize(identifier)
    for raw in _reference_leaves(metadata):
        if cap is not None and isinstance(raw, str) and len(raw) > max(0, cap):
            continue
        if isinstance(raw, str) and _normalize(raw) == normalized:
            return True
    return False


# Shapes the SQL walk and its value test must treat exactly as the Python reference does. The database
# the Mac app creates is --locale=C, where Postgres lower() folds ASCII only: the accented, Kelvin-sign and
# no-break-space rows are the ones a plain SQL lower() equality would miss.
_SHAPES: list[dict] = [
    {"sidecar": {"contract_id": "K-1"}},
    {"sidecar": {"contract": {"id": "K-2"}}},
    {"frontmatter": {"order_id": ["K-3", 7, {"id": "K-4"}, ["K-5"]]}},
    {"sidecar": {"id": {"id": "K-6"}}},
    {"sidecar": {"invoice_id": 763}},
    {"sidecar": {"ID": "K-7"}},
    {"sidecar": {"case_id\n": "K-8"}},
    {"sidecar": {"x\ny_id": "K-9"}},
    {"sidecar": {"vendor": "K-10"}},
    {"sidecar": "K-11"},
    {"frontmatter": ["K-12"]},
    {"tika": {"id": "K-13"}},
    {"id": "K-14"},
    {"sidecar": {"case_id": "  k-15 \t  PART\x1ctwo "}},
    {"sidecar": {"case_id": "K-16 B"}},
    {"frontmatter": {"id": "ÉCO-17"}},
    {"frontmatter": {"id": "K-18"}},
    {"frontmatter": {"id": "K-19　X"}},
    {"frontmatter": {"a": {"b": {"c": {"ref_id": "K-20"}}}}},
    {"sidecar": {"policy_id": "K_21%"}},
    {"sidecar": {"policy_id": "KX21Y"}},
    {"sidecar": {"message_id": " " * 8 + "K-22"}},
    {"sidecar": {"contract_id": None, "order_id": True}},
    {},
]
_SHAPE_IDENTIFIERS = [
    "K-1", "K-2", "K-3", "K-4", "K-5", "K-6", "763", "K-7", "K-8", "K-9", "K-10", "K-11", "K-12", "K-13",
    "K-14", "k-15 part two", "k-16 b", "éco-17", "ÉCO-17", "k-18", "k-19 x", "K-20", "k_21%", "K-22",
    "true", "null",
]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [None, 50, 5, 0])
async def test_metadata_pass_agrees_with_the_python_walk_on_every_shape(db_session, cap):
    """For every identifier, the documents the metadata pass returns are exactly those the reference walk
    matches, under no cap and under caps that drop some values (a value longer than the cap is never
    confirmed, #831). Titles carry none of the identifiers, so only the metadata pass can match."""
    docs = [await _seed_doc(db_session, title=f"Shape {i:02d}", metadata=meta) for i, meta in enumerate(_SHAPES)]
    await db_session.flush()

    for identifier in _SHAPE_IDENTIFIERS:
        expected = {d.doc_id for d, meta in zip(docs, _SHAPES, strict=True) if _reference_match(meta, identifier, cap)}
        found = {c.doc_id for c in await _find_candidates(db_session, identifier, key_snippet_cap=cap)}
        assert found == expected, (identifier, cap)
    # The rows a SQL lower() or [[:space:]] would get wrong do match: the reference is not vacuously empty.
    assert _reference_match(_SHAPES[15], "éco-17", None) and _reference_match(_SHAPES[16], "k-18", None)
    assert _reference_match(_SHAPES[13], "k-15 part two", None) and _reference_match(_SHAPES[14], "k-16 b", None)


@pytest.mark.asyncio
async def test_overflow_is_reported_only_when_a_candidate_was_left_out(db_session):
    """Exactly 100 matches all come back with no overflow; a 101st turns overflow on, and the one left out
    is the same on every call (candidates are ordered by title, then doc_id)."""
    for i in range(100):
        await _seed_doc(db_session, title=f"Doc {i:03d}", metadata={"frontmatter": {"id": "SHARED-763"}})
    await db_session.flush()

    result = await verify_identifier(db_session, "SHARED-763")
    assert result["status"] == "ambiguous" and result["count"] == 100
    assert "overflow" not in result and "More than 100" not in result["suggestion"]

    last = await _seed_doc(db_session, title="zzz last", metadata={"frontmatter": {"id": "SHARED-763"}})
    await db_session.flush()
    for _ in range(3):
        result = await verify_identifier(db_session, "SHARED-763")
        assert result["count"] == 100 and result["overflow"] is True
        assert str(last.doc_id) not in {c["doc_id"] for c in result["candidates"]}


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
async def test_ambiguous_with_nested_metadata_uses_nested_suggestion(db_session):
    """When two docs are ambiguous and the discriminating info is nested in a
    way the top-level projection can't see, the suggestion must mention
    'nested', NOT 'identical discriminating metadata'.

    Both docs match by title prefix. Doc A has nested metadata under
    sidecar.contract (a dict-valued leaf). Doc B has flat sidecar metadata
    on a different key. Because the path sets don't intersect, _find_differing
    _metadata_fields returns empty — but _has_nested_metadata detects the dict
    leaf on doc A and routes to the nested-aware suggestion branch.
    """
    # Doc A: nested sidecar metadata — sidecar.contract is a dict.
    await _seed_doc(
        db_session,
        title="Pinnacle Nested Alpha",
        metadata={"sidecar": {"contract": {"id": "K-2025-031"}}},
    )
    # Doc B: flat sidecar metadata on a DIFFERENT key — no common path with A.
    await _seed_doc(
        db_session,
        title="Pinnacle Nested Beta",
        metadata={"sidecar": {"vendor": "Acme Corp"}},
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Nested")
    assert result["status"] == "ambiguous"
    suggestion = result["suggestion"].lower()
    assert "nested" in suggestion
    assert "identical" not in suggestion


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


# ---------------------------------------------------------------------------
# documents_by_date response-shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_documents_by_date_response_shape(db_session):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Pinnacle Doc",
        metadata={"tika": {"created_at": "2024-06-15T12:00:00Z"}},
    )
    await db_session.flush()

    result = await documents_by_date(db_session, direction="earliest", limit=10)
    assert result["direction"] == "earliest"
    assert isinstance(result["count"], int)
    assert any(r["doc_id"] == str(target.doc_id) for r in result["results"])
    row = next(r for r in result["results"] if r["doc_id"] == str(target.doc_id))
    assert row["title"] == "Pinnacle Doc"
    assert row["date"] == "2024-06-15T12:00:00+00:00"
    assert row["date_source"] == "tika.created_at"


@pytest.mark.asyncio
async def test_documents_by_date_count_matches_results_length(db_session):
    for i in range(3):
        await _seed_doc_with_chunk(
            db_session,
            title=f"Doc {i}",
            metadata={"tika": {"created_at": f"2024-01-{i + 1:02d}T00:00:00Z"}},
        )
    await db_session.flush()

    result = await documents_by_date(db_session, direction="earliest", limit=2)
    assert result["count"] == 2
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_documents_by_date_empty_results(db_session):
    """No active docs at all — return {count: 0, results: []}, never an error."""
    result = await documents_by_date(db_session, direction="earliest", limit=10)
    assert result["direction"] == "earliest"
    # We can't assert count == 0 because other tests may seed docs in the
    # same DB session; instead assert the shape is non-error.
    assert "error" not in result
    assert "results" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_direction_returns_error(db_session):
    result = await documents_by_date(db_session, direction="sideways", limit=10)
    assert "error" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_date_field_returns_error(db_session):
    result = await documents_by_date(db_session, direction="earliest", date_field="bogus.field", limit=10)
    assert "error" in result
    assert "tika.created_at" in result["error"]  # accepted values listed


@pytest.mark.asyncio
async def test_documents_by_date_invalid_after_returns_error(db_session):
    result = await documents_by_date(db_session, direction="earliest", after="not-a-date", limit=10)
    assert "error" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_metadata_filter_returns_error(db_session):
    result = await documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"too.many.dots": "x"},
        limit=10,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# MCP-layer integration tests (Task 6)
# ---------------------------------------------------------------------------


@contextmanager
def _principal_in_context(user):
    token = _mcp_principal.set(Principal(type="user", id=user.user_id, role="admin"))
    try:
        yield
    finally:
        _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session, _engine, monkeypatch):
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.mark.asyncio
async def test_kb_verify_identifier_unique_end_to_end(client, admin_user, db_session, mock_session_factory):
    target = await _seed_doc(db_session, title="Singular Pinnacle Doc")
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_verify_identifier(identifier="Singular Pinnacle Doc")
    parsed = json.loads(raw)
    assert parsed["status"] == "unique"
    assert parsed["match"]["doc_id"] == str(target.doc_id)


@pytest.mark.asyncio
async def test_kb_documents_by_date_end_to_end(client, admin_user, db_session, mock_session_factory):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Earliest doc",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
        text="discussion of California regulators",
    )
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_documents_by_date(direction="earliest", query="California", limit=5)
    parsed = json.loads(raw)
    assert parsed["direction"] == "earliest"
    assert any(r["doc_id"] == str(target.doc_id) for r in parsed["results"])


@pytest.mark.asyncio
async def test_find_candidates_escapes_percent_wildcard(db_session):
    """An identifier with literal % must not act as a wildcard."""
    target = await _seed_doc(db_session, title="50% off coupon")
    distractor = await _seed_doc(db_session, title="50 percent reduction")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "50% off")
    doc_ids = {c.doc_id for c in candidates}
    assert target.doc_id in doc_ids
    assert distractor.doc_id not in doc_ids
    # Without the escape, the pattern would be "%50% off%" → matches any title
    # containing "50<anything> off".


@pytest.mark.asyncio
async def test_find_candidates_escapes_underscore_wildcard(db_session):
    """An identifier with literal _ must not act as SQL's any-char wildcard."""
    target = await _seed_doc(db_session, title="contract_2024")
    distractor = await _seed_doc(db_session, title="contracta2024")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "contract_2024")
    doc_ids = {c.doc_id for c in candidates}
    assert target.doc_id in doc_ids
    assert distractor.doc_id not in doc_ids
    # Without the escape, "_" matches any single character, so distractor
    # "contracta2024" would have matched.


@pytest.mark.asyncio
async def test_find_candidates_tika_title_match_does_not_wildcard(db_session):
    """tika.title match must be true case-insensitive equality, not ILIKE
    with wildcard semantics on the value."""
    target = await _seed_doc(
        db_session, title="some-other-title", metadata={"tika": {"title": "Pinnacle Vendor Contract"}}
    )
    distractor = await _seed_doc(
        db_session, title="some-other-distractor", metadata={"tika": {"title": "Pinnacle Vendor"}}
    )
    await db_session.flush()

    # Without the fix, identifier "Pinnacle%Contract" would match both via
    # ILIKE pattern semantics. With true equality, only target matches.
    candidates = await _find_candidates(db_session, "Pinnacle%Contract")
    doc_ids = {c.doc_id for c in candidates}
    # Neither doc has a tika.title literally equal to "pinnacle%contract", so neither matches via tika.
    # And the title/filename ILIKE escape (Task 3) ensures the literal "%" doesn't wildcard either.
    assert target.doc_id not in doc_ids
    assert distractor.doc_id not in doc_ids


@pytest.mark.asyncio
async def test_find_candidates_tika_title_exact_match_works(db_session):
    """Exact tika.title match still works after the fix."""
    target = await _seed_doc(db_session, title="raw-filename", metadata={"tika": {"title": "Pinnacle Vendor Contract"}})
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle Vendor Contract")
    doc_ids = {c.doc_id for c in candidates}
    assert target.doc_id in doc_ids


# ---------------------------------------------------------------------------
# email.* dispatch parity — _query_documents_by_date must honour email.*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_documents_by_date_email_from_address_filter(db_session):
    """kb_documents_by_date with email.from_address filter returns only
    docs with a matching email_from_address column.

    Critical Fix #2 regression test: before the fix, email.* keys fell
    through the JSONB-only path and silently returned zero rows.
    """
    from datetime import UTC, datetime

    a = Document(
        title="Alice email",
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        email_from_address="alice@firm.com",
        email_date_sent=datetime(2026, 1, 15, tzinfo=UTC),
        email_message_id="<dbd-a@test>",
    )
    b = Document(
        title="Bob email",
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        email_from_address="bob@firm.com",
        email_date_sent=datetime(2026, 1, 15, tzinfo=UTC),
        email_message_id="<dbd-b@test>",
    )
    db_session.add_all([a, b])
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"email.from_address": "alice@firm.com"},
        limit=10,
    )
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(a.doc_id) in doc_ids
    assert str(b.doc_id) not in doc_ids


# ---------------------------------------------------------------------------
# The word pass (#711): a display name against a filing name
# ---------------------------------------------------------------------------

ARCA = "ArcaUsTreasuryFund_20200207_N-2_EX-99.K5_11971930_EX-99.K5_Development Agreement"
CLICKSTREAM = "ClickstreamCorp_20200330_1-A_EX1A-6 MAT CTRCT_12089935_EX1A-6 MAT CTRCT_Development Agreement"


@pytest.mark.asyncio
async def test_a_display_name_resolves_to_its_filing_name_by_words(db_session):
    """No substring of the display name is the filing name, so the exact pass finds nothing; every word of
    it is in the filing name, so the word pass finds exactly the one document and says how."""
    target = await _seed_doc(db_session, title=ARCA)
    await _seed_doc(db_session, title=CLICKSTREAM)
    await db_session.flush()
    assert await _find_candidates(db_session, "Arca Us Treasury Fund Development Agreement") == []
    out = await verify_identifier(db_session, "Arca Us Treasury Fund Development Agreement")
    assert out["status"] == "unique" and out["match"]["doc_id"] == str(target.doc_id)
    assert out["matched_by"] == "words"
    assert "confirm its title" in out["instruction"], "a loose match is never handed over as an exact one"


@pytest.mark.asyncio
async def test_the_word_pass_survives_articles_punctuation_and_case(db_session):
    """The question says "the Arca US Treasury Fund development agreement"; a model may quote it, add a
    possessive or a full stop. All of these are the same name."""
    target = await _seed_doc(db_session, title=ARCA)
    await _seed_doc(db_session, title=CLICKSTREAM)
    await db_session.flush()
    for name in (
        "the Arca US Treasury Fund development agreement",
        "Arca US Treasury Fund Development Agreement.",
        "Arca US Treasury Fund's Development Agreement",
        "Arca US Treasury Fund (Development Agreement)",
        '"Arca US Treasury Fund Development Agreement"',
    ):
        out = await verify_identifier(db_session, name)
        assert out["status"] == "unique" and out["match"]["doc_id"] == str(target.doc_id), name


@pytest.mark.asyncio
async def test_the_word_pass_matches_words_not_substrings(db_session):
    """ "Arca" is not "Arcadia" and "US" is not inside "Industries": a substring match returned unique for the
    wrong document on both, and unique is what callers quote without checking."""
    await _seed_doc(db_session, title="ArcadiaBiosciences_20190315_10-K_EX-10.22_Development Agreement")
    await _seed_doc(db_session, title="Prudential_Industries_Trust_Business_Plus_20200101_Master Services Agreement")
    await db_session.flush()
    assert (await verify_identifier(db_session, "Arca Development Agreement"))["status"] == "not_found"
    assert (await verify_identifier(db_session, "US Trust Services Agreement"))["status"] == "not_found"
    assert (await verify_identifier(db_session, "Prudential Industries Trust services agreement"))["status"] == "unique"


def test_name_and_title_tokenisers():
    from harbor_clerk.mcp_lookup_tools import _name_tokens, _title_tokens

    assert _name_tokens("the Arca US Treasury Fund's development agreement.") == [
        "arca",
        "us",
        "treasury",
        "fund",
        "development",
        "agreement",
    ]
    assert _name_tokens("2 Themart Com Inc Agency Agreement")[0] == "2", "digits stay"
    assert _name_tokens("T-Mobile agreement") == ["t", "mobile", "agreement"], "one-letter words stay, on both sides"
    # A run a person says as one word is kept whole as well as split.
    assert {"paypal", "pay", "pal"} <= _title_tokens("PayPal_Services_Agreement.pdf")
    assert {"q4", "k5", "3m"} <= _title_tokens("Q4 K5 3M")
    assert _name_tokens("PayPal Q4 3M") == ["paypal", "q4", "3m"]
    # Accents fold on both sides.
    assert _name_tokens("Société Générale agreement") == ["societe", "generale", "agreement"]
    assert {"societe", "generale"} <= _title_tokens("Société_Générale_Agreement.pdf")
    assert _title_tokens("ArcaUsTreasuryFund_20200207_N-2_EX-99.K5_Development Agreement") >= {
        "arca",
        "us",
        "treasury",
        "fund",
        "20200207",
        "development",
        "agreement",
    }
    assert {"cns", "pharmaceuticals", "inc"} <= _title_tokens("CnsPharmaceuticalsInc_20200326_8-K")
    assert "arcadia" in _title_tokens("ArcadiaBiosciences") and "arca" not in _title_tokens("ArcadiaBiosciences")


@pytest.mark.asyncio
async def test_words_shared_by_several_documents_are_ambiguous_not_unique(db_session):
    a = await _seed_doc(db_session, title=ARCA)
    b = await _seed_doc(db_session, title=CLICKSTREAM)
    await db_session.flush()
    # "Development Agreement" alone is a substring of both titles, so the exact pass answers ambiguous itself;
    # reversed, no substring matches and the word pass finds both.
    out = await verify_identifier(db_session, "Agreement, Development")
    assert out["status"] == "ambiguous" and out["count"] == 2 and out["matched_by"] == "words"
    assert "confirm its title" in out["instruction"]
    assert {c["doc_id"] for c in out["candidates"]} == {str(a.doc_id), str(b.doc_id)}


@pytest.mark.asyncio
async def test_an_exact_match_wins_and_is_not_marked_loose(db_session):
    target = await _seed_doc(db_session, title=ARCA)
    await db_session.flush()
    out = await verify_identifier(db_session, "ArcaUsTreasuryFund_20200207")
    assert out["status"] == "unique" and out["match"]["doc_id"] == str(target.doc_id)
    assert "matched_by" not in out


@pytest.mark.asyncio
async def test_a_name_no_document_carries_is_still_not_found(db_session):
    await _seed_doc(db_session, title=ARCA)
    await db_session.flush()
    out = await verify_identifier(db_session, "Acme Widgets Master Services Agreement")
    assert out == {"status": "not_found", "identifier": "Acme Widgets Master Services Agreement"}


@pytest.mark.asyncio
async def test_the_word_pass_needs_two_words_and_matches_the_filename_too(db_session):
    """One word is the exact pass's own substring match, so the word pass declines it; the filename counts
    like the title does."""
    target = await _seed_doc(db_session, title="scan 0412", canonical_filename="Berkshire_Hills_Endorsement.pdf")
    await db_session.flush()
    assert await _find_candidates_by_words(db_session, "Berkshire") == []
    assert [d.doc_id for d in await _find_candidates_by_words(db_session, "Berkshire Hills endorsement")] == [
        target.doc_id
    ]
    assert await _find_candidates_by_words(db_session, "Berkshire Hills 50% off") == [], (
        "'off' is not a word of the name"
    )


@pytest.mark.asyncio
async def test_a_one_letter_word_of_the_name_still_counts(db_session):
    """ "T-Mobile agreement" must not resolve to Sprint's: dropping the "t" widened the name."""
    sprint = await _seed_doc(db_session, title="Sprint Mobile Agreement.pdf")
    await db_session.flush()
    assert (await verify_identifier(db_session, "T-Mobile agreement"))["status"] == "not_found"
    tm = await _seed_doc(db_session, title="T-Mobile_Agreement_2021.pdf")
    await db_session.flush()
    out = await verify_identifier(db_session, "T-Mobile agreement")
    assert out["status"] == "unique" and out["match"]["doc_id"] == str(tm.doc_id) != str(sprint.doc_id)


@pytest.mark.asyncio
async def test_a_run_said_as_one_word_and_an_accented_name_resolve(db_session):
    paypal = await _seed_doc(db_session, title="PayPal_Services_Agreement.pdf")
    sg = await _seed_doc(db_session, title="Société_Générale_Facility_Agreement.pdf")
    await db_session.flush()
    assert (await verify_identifier(db_session, "PayPal services agreement"))["match"]["doc_id"] == str(paypal.doc_id)
    assert (await verify_identifier(db_session, "Societe Generale facility agreement"))["match"]["doc_id"] == str(
        sg.doc_id
    )


@pytest.mark.asyncio
async def test_a_hundred_substring_decoys_do_not_hide_the_document_or_stand_in_for_it(db_session):
    """The SQL pass narrows by substring; a cap there ran before the token check and dropped the true document
    behind decoys that contain its words as fragments ("us" in "Business", "trust" in "Trustee"). The cap
    applies to what passed the token check."""
    for i in range(100):
        await _seed_doc(db_session, title=f"Business_Trustee_Agreement_{i:03d}.pdf")
    target = await _seed_doc(db_session, title="US Trust Agreement.pdf")  # sorts after every decoy
    await db_session.flush()
    out = await verify_identifier(db_session, "the US Trust agreement")
    assert out["status"] == "unique" and out["match"]["doc_id"] == str(target.doc_id)
    assert "overflow" not in out


@pytest.mark.asyncio
async def test_a_word_said_twice_is_still_one_word(db_session):
    """The two-word minimum counts distinct words: "Acme Acme" must not reach the word pass."""
    await _seed_doc(db_session, title="Acme_Corp_Master_Services_Agreement.pdf")
    await db_session.flush()
    assert (await verify_identifier(db_session, "Acme Acme"))["status"] == "not_found"
    assert (await verify_identifier(db_session, "Acme Corp"))["status"] == "unique"
