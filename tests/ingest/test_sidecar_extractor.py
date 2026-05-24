"""SidecarExtractor — loads <stem>.json next to source_path."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from harbor_clerk.ingest.metadata_extractors.sidecar import SidecarExtractor


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str


def test_sidecar_extractor_loads_json_next_to_source_path(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("invoice body text")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme", "total_usd": 1234.56}))

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"invoice body text",
        source_path=str(doc_file),
    )
    assert out == {"vendor": "Acme", "total_usd": 1234.56}


def test_sidecar_extractor_returns_none_when_no_sidecar_exists(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_returns_none_when_source_path_unset():
    """Legacy uploaded docs without a watched-folder source_path → no sidecar."""
    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=None,
    )
    assert out is None


def test_sidecar_extractor_logs_and_returns_none_on_malformed_json(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text("{ not valid json")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_returns_none_for_empty_sidecar(tmp_path: Path):
    """An empty JSON object {} → return None so the namespace isn't written."""
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text("{}")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_rejects_non_object_top_level(tmp_path: Path):
    """A top-level list or string in the sidecar is not a valid metadata
    dict — log warning, return None."""
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text('["not", "an", "object"]')

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None
