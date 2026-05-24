"""Tests for the metadata extractor framework: registration + run_all()."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from harbor_clerk.ingest.metadata_extractors import MetadataExtractor, _run_extractors


@dataclass
class _FakeDoc:
    """Stand-in for Document; only needs doc_id + title for the framework tests."""

    doc_id: uuid.UUID
    title: str = "fake"


class _AlphaExtractor:
    name = "alpha"

    def extract(self, *, doc, raw_bytes, source_path):
        return {"foo": "bar"}


class _BetaExtractor:
    name = "beta"

    def extract(self, *, doc, raw_bytes, source_path):
        return {"baz": "qux"}


class _SkippingExtractor:
    name = "skipper"

    def extract(self, *, doc, raw_bytes, source_path):
        return None  # signals "doesn't apply to this doc"


class _FailingExtractor:
    name = "broken"

    def extract(self, *, doc, raw_bytes, source_path):
        raise RuntimeError("intentionally broken")


def test_run_all_merges_namespaced_results():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _BetaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert out["alpha"] == {"foo": "bar"}
    assert out["beta"] == {"baz": "qux"}


def test_run_all_records_provenance_per_extractor():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    prov = out.get("_source_provenance", {})
    assert "alpha" in prov
    # ISO 8601 with offset
    parsed = datetime.fromisoformat(prov["alpha"])
    assert parsed.tzinfo is not None


def test_run_all_skips_extractors_that_return_none():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _SkippingExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert "alpha" in out
    assert "skipper" not in out
    assert "skipper" not in out.get("_source_provenance", {})


def test_run_all_isolates_extractor_failures(caplog):
    """A failing extractor logs a warning but does not abort the others."""
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _FailingExtractor(), _BetaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert "alpha" in out
    assert "beta" in out
    assert "broken" not in out
    # The warning landed
    assert any("broken" in rec.message for rec in caplog.records if rec.levelname == "WARNING")


def test_run_all_empty_when_no_extractors_match():
    """All extractors skip → empty dict, no provenance key."""
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors([_SkippingExtractor()], doc=doc, raw_bytes=b"", source_path=None)
    assert out == {}


def test_metadata_extractor_protocol_is_satisfied_by_duck_type():
    """MetadataExtractor is a @runtime_checkable Protocol — duck-typed mocks
    satisfy isinstance()."""
    assert isinstance(_AlphaExtractor(), MetadataExtractor)
