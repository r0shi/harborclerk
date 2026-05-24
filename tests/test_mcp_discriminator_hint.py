"""Unit tests for `_compute_discriminator_hint` and its helpers.

The discriminator surfaces a hint string to the model when top-K kb_search
hits span multiple docs whose relevance scores are close AND whose
structured metadata fields differ — pointing the model toward PR-F's
metadata_filter for disambiguation. The hint is absent (not null) when
any trigger condition fails.
"""

import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock

from harbor_clerk.mcp_discriminator import (
    _build_suggestion,
    _compute_discriminator_hint,
    _find_differing_metadata_fields,
)


@dataclass
class _FakeHit:
    """Minimal SearchHit stand-in — just the fields the discriminator reads."""

    doc_id: str
    doc_title: str
    score: float


def _fake_session_for(metadata_by_doc: dict[str, dict]):
    """Returns a MagicMock session whose .execute(...).all() yields rows
    with .doc_id and .doc_metadata for the keys in metadata_by_doc."""

    rows = []
    for did, meta in metadata_by_doc.items():
        row = MagicMock()
        row.doc_id = uuid.UUID(did)
        row.doc_metadata = meta
        rows.append(row)

    session = MagicMock()
    session.execute.return_value.all.return_value = rows
    return session


def test_compute_returns_none_when_fewer_than_two_hits():
    hits = [_FakeHit(doc_id=str(uuid.uuid4()), doc_title="only", score=0.9)]
    assert _compute_discriminator_hint(hits, _fake_session_for({})) is None


def test_compute_returns_none_when_all_hits_from_one_doc():
    did = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.9),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.85),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.8),
    ]
    assert _compute_discriminator_hint(hits, _fake_session_for({})) is None


def test_compute_returns_none_when_candidate_scores_too_far_apart():
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
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_none_when_candidates_have_all_same_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    same = {"sidecar": {"vendor": "Acme", "term_months": 12}}
    session = _fake_session_for({a: same, b: same})
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_none_when_candidates_have_empty_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for({a: {}, b: {}})
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_hint_when_candidates_differ_on_metadata():
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
    hint = _compute_discriminator_hint(hits, session)
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


def test_compute_orders_differing_fields_by_distinctness():
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
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    # `vendor` has 3 distinct values (X, Y, Z); `type` has 2 (A, B); `shared_field` has 1 (excluded)
    paths = list(hint["differing_metadata"].keys())
    assert paths[0] == "sidecar.vendor"  # most discriminating first
    assert "sidecar.type" in paths
    assert "sidecar.shared_field" not in paths  # all same → not differing


def test_compute_skips_source_provenance_namespace():
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
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert all(not p.startswith("_source_provenance") for p in paths)


def test_compute_skips_fields_missing_from_some_candidates():
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
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert "sidecar.only_in_a" not in paths


def test_find_differing_metadata_fields_returns_empty_for_identical_metadata():
    titles = {"a": "A", "b": "B"}
    metadata = {"a": {"sidecar": {"x": 1}}, "b": {"sidecar": {"x": 1}}}
    assert _find_differing_metadata_fields(["a", "b"], metadata, titles) == {}


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
