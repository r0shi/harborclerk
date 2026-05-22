"""Tests for sweep.py's ingest-detection helpers — particularly the
`_hc_corpus_matches` shortcut that lets `--resume` skip a re-ingest when
HC already has the right corpus loaded."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from scripts.test_corpora.corpora.manifest import CorpusManifest
from scripts.test_corpora.runner.client import HarborClerkClient
from scripts.test_corpora.runner.sweep import (
    _can_skip_ingest,
    _hc_corpus_matches,
    _is_retryable_research_failure,
    _wait_for_hc_reachable,
    main,
)


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


# ── retry decision logic ──


def test_is_retryable_when_research_failed():
    """status=failed → retry. Most common case: llama-server hiccup or
    model-not-warm-yet despite HC reporting ready."""
    assert _is_retryable_research_failure({"result": {"status": "failed"}}) is True


def test_is_retryable_when_research_interrupted():
    """status=interrupted → retry. SSE stream closed mid-run."""
    assert _is_retryable_research_failure({"result": {"status": "interrupted"}}) is True


def test_is_retryable_when_harness_aborted():
    """harness_aborted=True → retry. Watchdog forced the stream closed
    because the model went unhealthy. Retry covers auto-recovery."""
    assert (
        _is_retryable_research_failure(
            {
                "result": {
                    "status": "interrupted",
                    "harness_aborted": True,
                    "harness_abort_reason": "model state=loading",
                }
            }
        )
        is True
    )


def test_not_retryable_when_completed_with_answer():
    """Successful research must NEVER retry, even on flaky-looking results."""
    assert _is_retryable_research_failure({"result": {"status": "completed", "answer": "yes"}}) is False


def test_not_retryable_when_completed_empty_answer():
    """Completed-with-empty-answer is a model behaviour, not a transient.
    Same query against same model won't suddenly succeed — don't waste
    a retry slot."""
    assert _is_retryable_research_failure({"result": {"status": "completed", "answer": ""}}) is False


def test_not_retryable_when_no_result_block():
    """Phase 0/1 outputs don't have a result block; never retry those.
    (The caller already gates retry on _is_research, but the helper itself
    should be safe to call with anything.)"""
    assert _is_retryable_research_failure({}) is False
    assert _is_retryable_research_failure({"result": {}}) is False
    assert _is_retryable_research_failure({"result": None}) is False


# ── HC unreachability wait ──


def test_wait_for_hc_reachable_returns_true_on_first_success():
    """Happy path: HC's /system/health returns 200, helper returns True."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system/health":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(404)

    c = _make_client(handler)
    assert _wait_for_hc_reachable(c, max_wait_seconds=5) is True


def test_wait_for_hc_reachable_returns_true_after_recovery():
    """HC was down, comes back during the wait — return True so the caller
    knows to continue. The pattern this catches: model-switch restart of
    the HC API that completes in 6-15s."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system/health":
            calls["n"] += 1
            if calls["n"] <= 2:
                # First two probes fail with ConnectError-equivalent
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(404)

    c = _make_client(handler)
    # Plenty of budget so the helper can ride out the first two failures
    assert _wait_for_hc_reachable(c, max_wait_seconds=30) is True
    assert calls["n"] >= 3


def test_wait_for_hc_reachable_returns_false_on_persistent_outage():
    """HC stays down past the deadline → return False so the caller
    increments the outage counter (and eventually bails)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    c = _make_client(handler)
    # max_wait_seconds=0 means the deadline is already past — we exit
    # the polling loop immediately without sleeping.
    assert _wait_for_hc_reachable(c, max_wait_seconds=0) is False


# ── --no-ingest flag ──


def test_no_ingest_flag_never_calls_ingest_corpus(tmp_path: Path) -> None:
    """Phase-1 plan spanning cuad + enron: with --no-ingest, _ingest_corpus
    must never be called regardless of how many corpus transitions occur."""

    # Pre-populate a minimal workdir so main() doesn't need network access
    # for Phase 0 acquisition. The questions YAML files live in the real repo
    # and are loaded from disk by main() — we don't need to stub them.
    run_dir = tmp_path / "results" / "test-no-ingest"
    run_dir.mkdir(parents=True)

    # Fake HC client: health check succeeds, login is a no-op, MCP bearer
    # returns a token. All other calls (pipeline_quiet, document_count, etc.)
    # are silenced by MagicMock defaults.
    fake_hc = MagicMock()
    fake_hc.get_bearer_token.return_value = "fake-token"

    # Fake MCP session so Phase 1's SyncMcpSession construction is bypassed.
    fake_mcp = MagicMock()

    # Fake baseline result so _phase1_baseline returns without hitting Anthropic.
    fake_baseline_result = {
        "question_id": "cuad-research-1",
        "answer": "test answer",
        "cited_doc_ids": [],
        "named_entities": [],
        "elapsed_seconds": 1.0,
    }

    with (
        patch("scripts.test_corpora.runner.sweep.HarborClerkClient", return_value=fake_hc),
        patch("scripts.test_corpora.runner.sweep.anthropic.Anthropic"),
        patch("scripts.test_corpora.runner.sweep.JudgeClient"),
        patch("scripts.test_corpora.runner.sweep.SyncMcpSession", return_value=fake_mcp),
        patch("scripts.test_corpora.runner.sweep._phase1_baseline", return_value=fake_baseline_result),
        patch("scripts.test_corpora.runner.sweep._ingest_corpus") as mock_ingest,
    ):
        rc = main(
            [
                "--run-id",
                "test-no-ingest",
                "--workdir",
                str(tmp_path),
                "--phases",
                "1",
                "--corpora",
                "cuad,enron",
                "--no-ingest",
                "--no-hc-logs",
                "--skip-canary",
                "--no-judge",
            ]
        )

    assert rc == 0, "main() should exit 0 with --no-ingest"
    mock_ingest.assert_not_called(), "_ingest_corpus must never be called with --no-ingest"


def test_mode_answer_eval_is_accepted_and_dispatches(monkeypatch, tmp_path):
    """--mode answer-eval parses on the sweep parser and dispatches to
    answer_eval.main_from_args before the main sweep loop spins up."""
    from scripts.test_corpora.runner import answer_eval, sweep

    called = {}

    def fake_main_from_args(args):
        called["args"] = args
        return 0

    monkeypatch.setattr(answer_eval, "main_from_args", fake_main_from_args)
    rc = sweep.main(
        [
            "--run-id",
            "test-answer-eval",
            "--workdir",
            str(tmp_path),
            "--mode",
            "answer-eval",
            "--label",
            "lbl",
            "--corpora",
            "cuad",
            "--models",
            "claude-sonnet-4-6",
        ]
    )
    assert rc == 0
    assert called["args"].mode == "answer-eval"
    assert called["args"].label == "lbl"
