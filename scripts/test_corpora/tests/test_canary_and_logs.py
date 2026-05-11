"""Tests for the canary + HC log capture helpers in sweep.py."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.test_corpora.runner.sweep import _capture_hc_logs, _run_canary

# ── _run_canary ──


def _fake_hc(events: Iterable[dict], *, raise_on_create: bool = False, raise_on_stream: bool = False):
    """Construct a MagicMock HC client that returns ``events`` from stream_ask."""
    hc = MagicMock()
    if raise_on_create:
        hc.create_conversation.side_effect = RuntimeError("simulated HC down")
    else:
        hc.create_conversation.return_value = "conv-1"
    if raise_on_stream:
        hc.stream_ask.side_effect = RuntimeError("simulated stream failure")
    else:
        hc.stream_ask.return_value = events
    return hc


def test_canary_passes_on_real_answer() -> None:
    events = [
        {"type": "token", "content": "I found three documents: "},
        {"type": "token", "content": "Vendor Agreement, Master Services, and HR Policy."},
        {"type": "done"},
    ]
    hc = _fake_hc(events)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    assert out["passed"] is True
    assert out["label"] == "real"
    assert out["answer_chars"] > 0
    assert out["reason"] is None
    hc.create_conversation.assert_called_once()
    hc.stream_ask.assert_called_once()


def test_canary_fails_on_empty_stream() -> None:
    # Verbatim signature of the dead-LLM cascade: chat returns "completed"
    # with no token events in ~2s.
    events = [{"type": "done"}]
    hc = _fake_hc(events)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    assert out["passed"] is False
    assert out["label"] == "empty"
    assert out["answer_chars"] == 0


def test_canary_fails_on_refusal() -> None:
    events = [
        {"type": "token", "content": "I'm sorry, but I don't have the capability to search the documents."},
        {"type": "done"},
    ]
    hc = _fake_hc(events)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    assert out["passed"] is False
    assert out["label"] == "refusal"


def test_canary_fails_on_create_conversation_exception() -> None:
    hc = _fake_hc(events=[], raise_on_create=True)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    assert out["passed"] is False
    assert out["label"] == "exception"
    assert "simulated HC down" in str(out["reason"])


def test_canary_fails_on_stream_exception() -> None:
    hc = _fake_hc(events=[], raise_on_stream=True)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    assert out["passed"] is False
    assert out["label"] == "exception"
    assert "simulated stream failure" in str(out["reason"])


def test_canary_result_is_json_serialisable() -> None:
    # The result dict is persisted to ``canary.json`` in the run dir.
    # If anything in the dict isn't JSON-serialisable, the persistence
    # crashes the sweep at unit time, which is exactly the failure mode
    # the canary is supposed to head off.
    events = [{"type": "token", "content": "Hello."}, {"type": "done"}]
    hc = _fake_hc(events)
    out = _run_canary(hc, "cuad", logging.getLogger("test"))
    json.dumps(out)  # must not raise


# ── _capture_hc_logs ──


def test_capture_hc_logs_copies_log_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src_dir = tmp_path / "src_logs"
    src_dir.mkdir()
    (src_dir / "api.log").write_text("api line 1\n")
    (src_dir / "worker-io.log").write_text("worker line 1\n")
    (src_dir / "not-a-log.txt").write_text("ignored\n")

    monkeypatch.setenv("HC_LOG_DIR", str(src_dir))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    _capture_hc_logs(run_dir, logging.getLogger("test"))

    dst = run_dir / "hc_logs"
    assert dst.exists()
    assert (dst / "api.log").read_text() == "api line 1\n"
    assert (dst / "worker-io.log").read_text() == "worker line 1\n"
    # Only *.log files should be copied; .txt should be skipped.
    assert not (dst / "not-a-log.txt").exists()


def test_capture_hc_logs_handles_missing_source_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_LOG_DIR", str(tmp_path / "does_not_exist"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Must not raise — best-effort behavior is documented.
    _capture_hc_logs(run_dir, logging.getLogger("test"))
    assert not (run_dir / "hc_logs").exists()


def test_capture_hc_logs_respects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # When HC_LOG_DIR is set, it overrides the default macOS path.
    custom_src = tmp_path / "custom"
    custom_src.mkdir()
    (custom_src / "test.log").write_text("custom\n")

    monkeypatch.setenv("HC_LOG_DIR", str(custom_src))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    _capture_hc_logs(run_dir, logging.getLogger("test"))
    assert (run_dir / "hc_logs" / "test.log").read_text() == "custom\n"


def test_capture_hc_logs_creates_destination_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.log").write_text("data\n")
    monkeypatch.setenv("HC_LOG_DIR", str(src))

    # run_dir exists but no hc_logs subdir yet
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert not (run_dir / "hc_logs").exists()

    _capture_hc_logs(run_dir, logging.getLogger("test"))
    assert (run_dir / "hc_logs").is_dir()


def test_capture_hc_logs_no_env_var_uses_macos_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # When HC_LOG_DIR is absent, the function should fall through to the
    # macOS default. We can't test that path actually works (the directory
    # may or may not exist on the test machine), but we can verify the
    # absence-of-env-var branch doesn't crash.
    monkeypatch.delenv("HC_LOG_DIR", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Must not raise regardless of default-path existence.
    _capture_hc_logs(run_dir, logging.getLogger("test"))
