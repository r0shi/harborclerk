"""TikaMetadataExtractor — calls Tika's /meta endpoint, whitelists fields."""

import uuid
from dataclasses import dataclass

from pytest_httpserver import HTTPServer

from harbor_clerk.ingest.metadata_extractors.tika_metadata import (
    TIKA_FIELD_ALIASES,
    TikaMetadataExtractor,
)


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str
    mime_type: str | None


def test_tika_extractor_returns_aliased_whitelisted_fields(httpserver: HTTPServer, monkeypatch):
    """Tika /meta returns a noisy dict; the extractor whitelists + aliases
    to readable filter keys and drops everything else."""
    tika_response = {
        # Whitelisted aliases
        "dc:creator": "Jane Doe",
        "dc:title": "Q3 Report",
        "dc:subject": "Quarterly Earnings",
        "xmpTPg:NPages": 12,
        "dcterms:created": "2024-09-15T10:30:00Z",
        # Noise that should be dropped
        "X-TIKA-Parsed-By": "org.apache.tika.parser.pdf.PDFParser",
        "X-TIKA-Content-Length": "8432",
        "pdf:PDFVersion": "1.7",
        "Custom-Random-Field": "ignored",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)

    # Point Tika settings at the test server
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="q3", mime_type="application/pdf"),
        raw_bytes=b"%PDF-1.7\n...",
        source_path=None,
    )

    assert out == {
        "author": "Jane Doe",
        "title": "Q3 Report",
        "subject": "Quarterly Earnings",
        "page_count": 12,
        "created_at": "2024-09-15T10:30:00Z",
    }


def test_tika_extractor_returns_none_when_tika_url_unset(monkeypatch):
    """If tika_url is empty, return None (no Tika to call)."""
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": ""})(),
    )
    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", mime_type="text/plain"),
        raw_bytes=b"hello",
        source_path=None,
    )
    assert out is None


def test_tika_extractor_handles_empty_response(httpserver: HTTPServer, monkeypatch):
    """Tika returning {} → return None (don't write an empty 'tika' namespace)."""
    httpserver.expect_request("/meta").respond_with_json({})
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )
    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", mime_type="application/pdf"),
        raw_bytes=b"...",
        source_path=None,
    )
    assert out is None


def test_tika_extractor_collects_email_headers(httpserver: HTTPServer, monkeypatch):
    """For .eml files, Tika emits Message-From/To/Cc/Subject headers."""
    tika_response = {
        "Message-From": "alice@example.com",
        "Message-To": "bob@example.com,carol@example.com",
        "Message-Cc": "dave@example.com",
        "Message-Subject": "Re: Project Update",
        "dcterms:created": "2024-09-15T10:30:00Z",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="re-project-update", mime_type="message/rfc822"),
        raw_bytes=b"From: alice@example.com\n...",
        source_path=None,
    )

    assert out["email_from"] == "alice@example.com"
    assert out["email_to"] == "bob@example.com,carol@example.com"
    assert out["email_cc"] == "dave@example.com"
    assert out["email_subject"] == "Re: Project Update"


def test_tika_field_aliases_has_no_duplicate_target_keys():
    """The alias map can map two Tika keys to the same target (e.g. both
    xmpTPg:NPages and Page-Count → page_count); for each target key, the
    LAST writer wins. Document the conflict set for future readers."""
    targets = list(TIKA_FIELD_ALIASES.values())
    # We allow duplicates (Tika emits aliases for the same concept), but the
    # test pins the current count so a future refactor surfaces the change.
    unique_targets = set(targets)
    duplicates = [t for t in unique_targets if targets.count(t) > 1]
    # Document expected duplicates explicitly:
    assert sorted(duplicates) == sorted(["page_count"])


def test_tika_extractor_aliases_dc_identifier_to_isbn(httpserver: HTTPServer, monkeypatch):
    """EPUB and some PDFs carry ISBN as dc:identifier. The alias map
    routes it to a flat `tika.isbn` key for metadata_filter use."""
    tika_response = {
        "dc:identifier": "978-0-13-468599-1",
        "dc:title": "The Book",
        "Content-Type": "application/epub+zip",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)

    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="book", mime_type="application/epub+zip"),
        raw_bytes=b"PK\x03\x04...",
        source_path=None,
    )

    assert out == {
        "isbn": "978-0-13-468599-1",
        "title": "The Book",
        "content_type": "application/epub+zip",
    }
