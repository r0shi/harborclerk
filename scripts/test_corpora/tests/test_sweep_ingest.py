"""Tests for sweep.py's ingest-detection helpers — particularly the
`_hc_corpus_matches` shortcut that lets `--resume` skip a re-ingest when
HC already has the right corpus loaded."""

from __future__ import annotations

from pathlib import Path

import httpx

from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.sweep import _can_skip_ingest, _hc_corpus_matches


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


def test_can_skip_ingest_true_when_quiet_and_loaded():
    """Happy path for --resume: queue is quiet, folder + count match → True."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/jobs/snapshot":
            return httpx.Response(
                200, json={"queues": {"io": {"queued": 0, "running": 0}, "cpu": {"queued": 0, "running": 0}}}
            )
        if request.url.path == "/api/watch/folders":
            return httpx.Response(200, json=[{"folder_id": "f1", "path": "/tmp/cuad-ingest"}])
        if request.url.path == "/api/docs":
            return httpx.Response(200, json={"items": [], "total": 80, "limit": 0})
        return httpx.Response(404)

    c = _make_client(handler)
    assert _can_skip_ingest(c, _manifest(), max_drain_wait=0) is True


def test_can_skip_ingest_false_when_queue_busy_and_no_drain_budget():
    """Mid-ingest case the user flagged: HC has 51% of expected docs but
    the queue is still draining. With max_drain_wait=0 the helper bails
    out and returns False, forcing _ingest_corpus to wipe + re-ingest
    rather than letting research start against a partial corpus."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/jobs/snapshot":
            return httpx.Response(
                200, json={"queues": {"io": {"queued": 5, "running": 1}, "cpu": {"queued": 0, "running": 0}}}
            )
        return httpx.Response(404)

    c = _make_client(handler)
    assert _can_skip_ingest(c, _manifest(), max_drain_wait=0) is False
    # Critical: with the queue busy we must NEVER read document_count and decide
    # based on a moving number. Verify the helper short-circuited before
    # calling /api/docs or /api/watch/folders.
    assert "/api/docs" not in seen_paths
    assert "/api/watch/folders" not in seen_paths


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
