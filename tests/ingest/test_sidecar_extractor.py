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


def test_run_all_reads_a_sidecar_only_beside_the_in_place_path(tmp_path: Path, monkeypatch):
    """run_all hands the extractors the in-place path and nothing else."""
    from harbor_clerk.ingest import metadata_extractors

    monkeypatch.setattr(metadata_extractors, "EXTRACTORS", [SidecarExtractor()])
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    (tmp_path / "0001_invoice.json").write_text(json.dumps({"vendor": "Acme"}))
    doc = _FakeDoc(doc_id=uuid.uuid4(), title="i")

    assert metadata_extractors.run_all(doc=doc, raw_bytes=b"body", in_place_path=None) == {}
    in_place = metadata_extractors.run_all(doc=doc, raw_bytes=b"body", in_place_path=str(doc_file))
    assert in_place["sidecar"] == {"vendor": "Acme"}


def test_verified_in_place_path_is_the_watched_path_only_when_source_path_matches(tmp_path: Path):
    from harbor_clerk.watched_source import verified_in_place_path

    folder = tmp_path / "folder"
    (folder / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(folder, target_is_directory=True)
    watched = str(folder.resolve() / "sub" / "a.txt")
    rows = [(str(folder), "sub/a.txt")]

    assert verified_in_place_path(rows, watched) == watched
    # The folder's path is the realpath'd one or the stored one; a symlinked folder still matches.
    assert verified_in_place_path([(str(link), "sub/a.txt")], watched) == watched
    assert verified_in_place_path([(str(link), "sub/a.txt")], str(link / "sub" / "a.txt")) == str(
        link / "sub" / "a.txt"
    )
    # Anything else is a label: no path.
    assert verified_in_place_path(rows, str(tmp_path / "elsewhere" / "a.txt")) is None
    assert verified_in_place_path(rows, None) is None
    assert verified_in_place_path([], watched) is None
    # A relative path that is not normal matches nothing, even when the strings line up.
    escaping = str(folder.resolve() / "sub" / ".." / ".." / "a.txt")
    assert verified_in_place_path([(str(folder), "sub/../../a.txt")], escaping) is None


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


def test_sidecar_extractor_rejects_oversize_sidecar(tmp_path: Path):
    """Pathological large sidecars (>64 KB) get rejected with a warning,
    not silently bloating the doc_metadata JSONB column."""
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    # 65 KB JSON payload — valid JSON, just too big
    big_value = "x" * 65_000
    sidecar.write_text(json.dumps({"too_big": big_value}))

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_handles_utf8_bom(tmp_path: Path):
    """Windows/Excel-exported sidecars often have a UTF-8 BOM; accept them
    cleanly rather than silently dropping the metadata."""
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_bytes(b'\xef\xbb\xbf{"vendor": "Acme"}')
    (tmp_path / "0001_invoice.txt").write_text("body")

    out = SidecarExtractor().extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(tmp_path / "0001_invoice.txt"),
    )
    assert out == {"vendor": "Acme"}


def test_verified_in_place_path_touches_no_disk_on_a_plain_match(monkeypatch):
    """realpath (which can block on a stalled mount) runs only when the plain path misses."""
    import os

    from harbor_clerk.watched_source import verified_in_place_path

    def no_realpath(path, *args, **kwargs):
        raise AssertionError(f"realpath called for {path}")

    monkeypatch.setattr(os.path, "realpath", no_realpath)
    rows = [("/srv/watched", "sub/a.txt")]
    assert verified_in_place_path(rows, "/srv/watched/sub/a.txt") == "/srv/watched/sub/a.txt"


def test_an_absolute_or_dot_dot_relative_path_verifies_nothing():
    """os.path.join would discard the folder for an absolute relative path; neither it
    nor a ``..`` component may produce a path that verifies."""
    from harbor_clerk.watched_source import canonical_source_path, verified_in_place_path

    assert verified_in_place_path([("/srv/watched", "/etc/hosts")], "/etc/hosts") is None
    assert verified_in_place_path([("/srv/watched", "a/../../etc/hosts")], "/srv/watched/a/../../etc/hosts") is None
    assert canonical_source_path("/srv/watched", "/etc/hosts") is None
    assert canonical_source_path("/srv/watched", "../hosts") is None
    assert canonical_source_path("/srv/watched", "sub/a.txt") == "/srv/watched/sub/a.txt"


def test_a_folder_path_that_is_not_normalized_still_verifies(monkeypatch):
    """An auto-discovered folder may be stored as e.g. /data/../watch: the watcher's
    paths are built on that string, and a normalized source_path verifies too,
    without touching the disk (no realpath)."""
    import os

    from harbor_clerk.watched_source import verified_in_place_path

    def no_realpath(path, *args, **kwargs):
        raise AssertionError(f"realpath called for {path}")

    monkeypatch.setattr(os.path, "realpath", no_realpath)

    rows = [("/data/../watch", "sub/a.txt")]
    assert verified_in_place_path(rows, "/data/../watch/sub/a.txt") == "/data/../watch/sub/a.txt"
    assert verified_in_place_path(rows, "/watch/sub/a.txt") == "/watch/sub/a.txt"
    monkeypatch.undo()
    assert verified_in_place_path([("/data/../watch", "../etc/hosts")], "/data/../etc/hosts") is None
