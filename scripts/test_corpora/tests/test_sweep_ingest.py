"""Tests for sweep.py's ingest-detection helpers — particularly the
`_hc_corpus_matches` shortcut that lets `--resume` skip a re-ingest when
HC already has the right corpus loaded."""

from __future__ import annotations

from pathlib import Path

import httpx

from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.sweep import _hc_corpus_matches


def _make_client(handler) -> HarborClerkClient:
    transport = httpx.MockTransport(handler)
    return HarborClerkClient(base_url="https://localhost", transport=transport, verify=False)


def _manifest(ingest_dir: str = "/tmp/cuad-ingest", doc_count: int = 80) -> CorpusManifest:
    return CorpusManifest(
        corpus_id="cuad",
        ingest_dir=Path(ingest_dir),
        doc_count=doc_count,
        total_size_bytes=1,
        license="ignored",
    )


def test_hc_corpus_matches_true_when_folder_and_count_align():
    """Happy-path: HC has the right watch folder AND a plausible doc count.
    Returns True so the caller skips the wipe-and-re-ingest cycle."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/cuad-ingest"}])
        if request.url.path == "/api/docs":
            assert request.url.params.get("limit") == "0"
            return httpx.Response(200, json={"items": [], "total": 80, "limit": 0, "offset": 0})
        return httpx.Response(404)

    c = _make_client(handler)
    assert _hc_corpus_matches(c, _manifest()) is True


def test_hc_corpus_matches_false_when_no_watch_folders():
    """Empty watch folder list → no corpus loaded, fall through to re-ingest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    c = _make_client(handler)
    assert _hc_corpus_matches(c, _manifest()) is False


def test_hc_corpus_matches_false_when_folder_path_differs():
    """HC has a watch folder but for a different corpus → don't skip ingest.
    Important: prevents a previous corpus's stale ingest from masquerading
    as this corpus's data."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/enron-ingest"}])
        return httpx.Response(404)

    c = _make_client(handler)
    assert _hc_corpus_matches(c, _manifest("/tmp/cuad-ingest")) is False


def test_hc_corpus_matches_false_when_doc_count_below_threshold():
    """Folder matches but HC has nowhere near the expected doc count —
    likely a partial / interrupted ingest. Re-ingest from scratch."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/cuad-ingest"}])
        if request.url.path == "/api/docs":
            # 39 < 80 * 0.5 = 40, so below the threshold
            return httpx.Response(200, json={"items": [], "total": 39, "limit": 0, "offset": 0})
        return httpx.Response(404)

    c = _make_client(handler)
    assert _hc_corpus_matches(c, _manifest(doc_count=80)) is False


def test_hc_corpus_matches_handles_tiny_corpus_floor():
    """A 1-document corpus's threshold floors at 1 (50% of 1 = 0 by int()).
    Without the `max(1, ...)` floor, an empty DB would falsely match every
    1-doc manifest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/x"}])
        if request.url.path == "/api/docs":
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 0, "offset": 0})
        return httpx.Response(404)

    c = _make_client(handler)
    m = CorpusManifest(corpus_id="x", ingest_dir=Path("/tmp/x"), doc_count=1, total_size_bytes=1, license="ignored")
    assert _hc_corpus_matches(c, m) is False
