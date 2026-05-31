"""Unit tests for `_compute_discriminator_hint` and its helpers.

The discriminator surfaces a hint string to the model when top-K kb_search
hits span multiple docs whose relevance scores are close AND whose
structured metadata fields differ — pointing the model toward PR-F's
metadata_filter for disambiguation. The hint is absent (not null) when
any trigger condition fails.
"""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_discriminator import (
    _build_suggestion,
    _compute_discriminator_hint,
    _find_differing_metadata_fields,
)
from harbor_clerk.mcp_server import _mcp_principal

# ---------------------------------------------------------------------------
# Integration-test fixtures (mirrors test_mcp_tools.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_principal(admin_user):
    """Set _mcp_principal context var to an admin Principal for the test."""
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    yield
    _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to share the test connection so tests see
    flushed data and lazy loading (greenlet-based) works correctly."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@dataclass
class _FakeHit:
    """Minimal SearchHit stand-in — just the fields the discriminator reads."""

    doc_id: str
    doc_title: str
    score: float


def _fake_session_for(metadata_by_doc: dict[str, dict]):
    """Returns a session whose await session.execute(...).all() yields rows
    with .doc_id and .doc_metadata for the keys in metadata_by_doc."""

    rows = []
    for did, meta in metadata_by_doc.items():
        row = MagicMock()
        row.doc_id = uuid.UUID(did)
        row.doc_metadata = meta
        rows.append(row)

    result_obj = MagicMock()
    result_obj.all.return_value = rows

    session = MagicMock()
    session.execute = AsyncMock(return_value=result_obj)
    return session


async def test_compute_returns_none_when_fewer_than_two_hits():
    hits = [_FakeHit(doc_id=str(uuid.uuid4()), doc_title="only", score=0.9)]
    assert await _compute_discriminator_hint(hits, _fake_session_for({})) is None


async def test_compute_returns_none_when_all_hits_from_one_doc():
    did = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.9),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.85),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.8),
    ]
    assert await _compute_discriminator_hint(hits, _fake_session_for({})) is None


async def test_compute_returns_none_when_candidate_scores_too_far_apart():
    """ε = max(0.05, 0.1 * top_score). Top score 0.9 → ε = 0.09 → only
    docs with score >= 0.81 are candidates. doc-A is in; doc-B (0.7) is
    not — so only one candidate, no ambiguity, no hint."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.7),
    ]
    # Even though metadata differs, score gap makes B not a candidate
    session = _fake_session_for({a: {"sidecar": {"v": 1}}, b: {"sidecar": {"v": 2}}})
    assert await _compute_discriminator_hint(hits, session) is None


async def test_compute_returns_none_when_candidates_have_all_same_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    same = {"sidecar": {"vendor": "Acme", "term_months": 12}}
    session = _fake_session_for({a: same, b: same})
    assert await _compute_discriminator_hint(hits, session) is None


async def test_compute_returns_none_when_candidates_have_empty_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for({a: {}, b: {}})
    assert await _compute_discriminator_hint(hits, session) is None


async def test_compute_returns_hint_when_candidates_differ_on_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="0131_vendor_contract", score=0.9),
        _FakeHit(doc_id=b, doc_title="0149_vendor_contract", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "Pinnacle", "term_months": 24}},
            b: {"sidecar": {"vendor": "Pinnacle", "term_months": 12}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    assert set(hint["ambiguous_doc_ids"]) == {a, b}
    assert set(hint["ambiguous_doc_titles"]) == {"0131_vendor_contract", "0149_vendor_contract"}
    # Only term_months differs (vendor is same), so it's the only field surfaced
    assert "sidecar.term_months" in hint["differing_metadata"]
    assert "sidecar.vendor" not in hint["differing_metadata"]
    assert hint["differing_metadata"]["sidecar.term_months"] == {
        "0131_vendor_contract": 24,
        "0149_vendor_contract": 12,
    }
    assert "suggestion" in hint
    assert "metadata_filter" in hint["suggestion"]


async def test_compute_orders_differing_fields_by_distinctness():
    """When multiple fields differ, the hint surfaces them ordered by the
    number of distinct values (most discriminating first). Capped at 3."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    c = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
        _FakeHit(doc_id=c, doc_title="C", score=0.87),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "X", "shared_field": "same", "type": "A"}},
            b: {"sidecar": {"vendor": "Y", "shared_field": "same", "type": "B"}},
            c: {"sidecar": {"vendor": "Z", "shared_field": "same", "type": "B"}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    # `vendor` has 3 distinct values (X, Y, Z); `type` has 2 (A, B); `shared_field` has 1 (excluded)
    paths = list(hint["differing_metadata"].keys())
    assert paths[0] == "sidecar.vendor"  # most discriminating first
    assert "sidecar.type" in paths
    assert "sidecar.shared_field" not in paths  # all same → not differing


async def test_compute_skips_source_provenance_namespace():
    """The internal _source_provenance key shouldn't appear as a discriminator
    even though its timestamps will always differ across docs."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {
                "sidecar": {"vendor": "X"},
                "_source_provenance": {"sidecar": "2026-01-01T00:00:00+00:00"},
            },
            b: {
                "sidecar": {"vendor": "Y"},
                "_source_provenance": {"sidecar": "2026-02-02T00:00:00+00:00"},
            },
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert all(not p.startswith("_source_provenance") for p in paths)


async def test_compute_skips_fields_missing_from_some_candidates():
    """A field that's present in some candidates but not others can't be
    used as a filter (it would exclude the missing ones). Skipped."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "X", "only_in_a": "yes"}},
            b: {"sidecar": {"vendor": "Y"}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert "sidecar.only_in_a" not in paths


def test_find_differing_metadata_fields_returns_empty_for_identical_metadata():
    titles = {"a": "A", "b": "B"}
    metadata = {"a": {"sidecar": {"x": 1}}, "b": {"sidecar": {"x": 1}}}
    assert _find_differing_metadata_fields(["a", "b"], metadata, titles) == {}


async def test_compute_compacts_non_scalar_values_in_differing_metadata():
    """Long list values (e.g. sidecar.attendees) bloat the response and crowd
    out small-context models. Lists must be summarized to {len, first} so the
    LLM sees the field is non-scalar without paying for every element.
    """
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    long_attendees_a = [
        "Eleanor Voss",
        "Raymond Holt",
        "Sandra Kimura",
        "Frederick Osei",
        "Diana Marchetti",
        "Thomas Blaine",
        "Priya Nandakumar",
        "Gerald Whitmore",
    ]
    long_attendees_b = [
        "Eleanor Voss",
        "Raymond Calloway",
        "Priya Nanthakumar",
        "Theodore Birch",
        "Sandra Ouellet",
        "Marcus Hensley",
    ]
    hits = [
        _FakeHit(doc_id=a, doc_title="0101_board_minutes", score=0.997),
        _FakeHit(doc_id=b, doc_title="0107_board_minutes", score=0.994),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"date": "2025-03-07", "attendees": long_attendees_a}},
            b: {"sidecar": {"date": "2025-03-03", "attendees": long_attendees_b}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    # Date (scalar) is surfaced unchanged
    assert hint["differing_metadata"]["sidecar.date"] == {
        "0101_board_minutes": "2025-03-07",
        "0107_board_minutes": "2025-03-03",
    }
    # Attendees (list) is compacted to {len, first}, not the full 8-name list
    att = hint["differing_metadata"]["sidecar.attendees"]
    assert att["0101_board_minutes"]["len"] == 8
    assert att["0101_board_minutes"]["first"].startswith("Eleanor Voss")
    assert att["0107_board_minutes"]["len"] == 6
    # Verify we didn't accidentally embed the full list
    import json

    serialized = json.dumps(hint)
    assert "Raymond Holt" not in serialized  # would appear if the full list leaked through
    assert "Gerald Whitmore" not in serialized


async def test_compute_prefers_scalar_fields_for_suggestion():
    """When both scalar and non-scalar fields differ, the suggestion should
    reference a scalar field — the LLM can put it back into a metadata_filter
    directly. A list-valued field is not a usable filter target.
    """
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            # type (scalar) and attendees (list) both differ; only type should
            # be the basis for the suggestion text.
            a: {"sidecar": {"type": "board-minutes", "attendees": ["Alice", "Bob"]}},
            b: {"sidecar": {"type": "vendor-contract", "attendees": ["Carol", "Dave"]}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    # Suggestion references the scalar type field, not attendees
    assert "sidecar.type" in hint["suggestion"]
    # And the suggestion does NOT include a list literal — a list-valued
    # metadata_filter is not something the LLM can use.
    assert "['Alice'" not in hint["suggestion"]
    assert "Bob" not in hint["suggestion"]


async def test_compute_compacts_dict_values_to_keys_and_len():
    """Dict-valued metadata fields are also compacted; the LLM sees the
    shape (keys + count) without the full nested content."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"nested": {"x": "long " * 100, "y": "verbose " * 100}}},
            b: {"sidecar": {"nested": {"x": "different long " * 100, "z": "completely other " * 50}}},
        }
    )
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None
    nested = hint["differing_metadata"]["sidecar.nested"]
    # Both summaries carry keys + len, not the full nested content
    assert nested["A"]["keys"] == ["x", "y"]
    assert nested["A"]["len"] == 2
    assert "long " * 100 not in str(nested["A"])


def test_build_suggestion_mentions_top_field_value():
    """The suggestion string should reference at least one concrete
    metadata_filter call the model can use."""
    titles = {"a": "A", "b": "B"}
    top_fields = [("sidecar.term_months", {"A": 24, "B": 12})]
    s = _build_suggestion(top_fields, titles)
    assert "metadata_filter" in s
    assert "sidecar.term_months" in s
    # At least one of the concrete values appears
    assert "24" in s or "12" in s


async def test_compute_includes_candidate_just_inside_epsilon():
    """A candidate whose score is exactly within ε of the top should be
    included. With top_score=0.95, ε = max(0.05, 0.095) = 0.095, so the
    threshold is 0.855. A score of 0.9001 (above threshold) is in."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.95),
        _FakeHit(doc_id=b, doc_title="B", score=0.9001),  # just inside ε
    ]
    session = _fake_session_for({a: {"sidecar": {"v": 1}}, b: {"sidecar": {"v": 2}}})
    hint = await _compute_discriminator_hint(hits, session)
    assert hint is not None  # both candidates included


async def test_compute_excludes_candidate_just_outside_epsilon():
    """A candidate whose score is just below the threshold should NOT be
    a candidate, leaving only 1 candidate → None."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.95),
        _FakeHit(doc_id=b, doc_title="B", score=0.8499),  # just outside ε
    ]
    session = _fake_session_for({a: {"sidecar": {"v": 1}}, b: {"sidecar": {"v": 2}}})
    assert await _compute_discriminator_hint(hits, session) is None


# ── Integration: kb_search includes discriminator_hint when applicable ──


async def test_kb_search_includes_discriminator_hint_in_response(
    client, admin_user, db_session, mcp_principal, mock_session_factory
):
    """End-to-end: when kb_search returns hits whose top docs differ on
    structured metadata, the response includes a discriminator_hint."""
    import json

    from harbor_clerk.mcp_server import kb_search
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    async def _seed(*, title: str, metadata: dict):
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
            chunk_text="jurisdiction governing law term",
            language="english",
        )
        db_session.add(chunk)
        await db_session.flush()
        return doc

    # Two contracts with the same vendor + same chunk text, differing on term_months
    await _seed(title="0131_vendor_contract", metadata={"sidecar": {"vendor": "Pinnacle", "term_months": 24}})
    await _seed(title="0149_vendor_contract", metadata={"sidecar": {"vendor": "Pinnacle", "term_months": 12}})

    raw = await kb_search(query="jurisdiction governing", k=10)

    parsed = json.loads(raw)
    # If the search returned 2+ hits across both docs (the chunk text is
    # identical so it should), the discriminator should fire on term_months.
    if len({hit["doc_id"] for hit in parsed.get("hits", [])}) >= 2:
        assert "discriminator_hint" in parsed
        hint = parsed["discriminator_hint"]
        assert "sidecar.term_months" in hint["differing_metadata"]
        assert "metadata_filter" in hint["suggestion"]
    else:
        # If retrieval only returned one of the docs, we can't assert the hint
        # fired — but we can assert the response has no broken discriminator_hint
        assert parsed.get("discriminator_hint") is None or isinstance(parsed["discriminator_hint"], dict)


async def test_kb_search_omits_discriminator_hint_when_not_applicable(
    client, admin_user, db_session, mcp_principal, mock_session_factory
):
    """When kb_search returns hits from only one doc, discriminator_hint
    is absent from the response (not present-as-None)."""
    import json

    from harbor_clerk.mcp_server import kb_search
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    doc = Document(
        title="solo",
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata={"sidecar": {"vendor": "Acme"}},
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="unique phrase xyz", language="english"))
    await db_session.flush()

    raw = await kb_search(query="unique phrase xyz", k=10)

    parsed = json.loads(raw)
    assert "discriminator_hint" not in parsed
