"""Unit tests for src/harbor_clerk/metadata_dates.py::effective_date()."""

from datetime import UTC, datetime

from harbor_clerk.metadata_dates import effective_date


class _Doc:
    """Stand-in for the Document model — only fields effective_date reads.

    Using a tiny dataclass-like helper instead of importing Document keeps
    this file a pure unit test (no DB, no SQLAlchemy fixtures).
    """

    def __init__(self, doc_metadata=None, created_at=None):
        self.doc_metadata = doc_metadata
        self.created_at = created_at


def test_uses_tika_when_present():
    doc = _Doc(doc_metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}})
    dt, label = effective_date(doc)
    assert dt == datetime(1999, 10, 13, 8, 34, tzinfo=UTC)
    assert label == "tika.created_at"


def test_prefers_tika_over_frontmatter_and_sidecar():
    doc = _Doc(
        doc_metadata={
            "tika": {"created_at": "2020-01-01T00:00:00Z"},
            "frontmatter": {"date": "2010-01-01"},
            "sidecar": {"date": "2015-01-01"},
        }
    )
    dt, label = effective_date(doc)
    assert dt.year == 2020
    assert label == "tika.created_at"


def test_falls_through_to_frontmatter():
    doc = _Doc(doc_metadata={"frontmatter": {"date": "2025-04-19"}})
    dt, label = effective_date(doc)
    assert dt == datetime(2025, 4, 19, tzinfo=UTC)
    assert label == "frontmatter.date"


def test_falls_through_to_sidecar():
    doc = _Doc(doc_metadata={"sidecar": {"date": "2024-08-01T12:00:00+00:00"}})
    dt, label = effective_date(doc)
    assert dt == datetime(2024, 8, 1, 12, 0, tzinfo=UTC)
    assert label == "sidecar.date"


def test_falls_through_to_ingest():
    ingest = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    doc = _Doc(doc_metadata={}, created_at=ingest)
    dt, label = effective_date(doc)
    assert dt == ingest
    assert label == "ingest"


def test_iso_without_timezone_assumed_utc():
    doc = _Doc(doc_metadata={"tika": {"created_at": "2024-01-15T10:00:00"}})
    dt, _ = effective_date(doc)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2024, 1, 15, 10, 0, tzinfo=UTC)


def test_date_only_string_parses():
    doc = _Doc(doc_metadata={"frontmatter": {"date": "2024-01-15"}})
    dt, _ = effective_date(doc)
    assert dt == datetime(2024, 1, 15, tzinfo=UTC)


def test_naive_datetime_object_assumed_utc():
    doc = _Doc(doc_metadata={"sidecar": {"date": datetime(2024, 2, 2)}})
    dt, _ = effective_date(doc)
    assert dt == datetime(2024, 2, 2, tzinfo=UTC)


def test_unparseable_string_falls_through_to_next_source():
    doc = _Doc(
        doc_metadata={"tika": {"created_at": "not-a-date"}},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    dt, label = effective_date(doc)
    assert label == "ingest"
    assert dt.year == 2026


def test_all_sources_missing_returns_none_and_none_label():
    doc = _Doc(doc_metadata={}, created_at=None)
    dt, label = effective_date(doc)
    assert dt is None
    assert label == "none"


def test_none_doc_metadata_falls_through_to_ingest():
    """doc_metadata=None should not raise — treat as empty dict."""
    ingest = datetime(2026, 1, 1, tzinfo=UTC)
    doc = _Doc(doc_metadata=None, created_at=ingest)
    dt, label = effective_date(doc)
    assert label == "ingest"
    assert dt == ingest
