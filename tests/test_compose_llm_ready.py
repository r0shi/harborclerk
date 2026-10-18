"""Under Docker Compose a model whose file is present but that is not loaded does not stall summaries (#654).

The active model follows its file being in the volume, and a present file can still fail to load (too
big for the container, a hand copy cut short): llama-server then exits and restarts while the model looks
active. A summary used to spend ~95 s of ConnectError retries per document against it. With
`llm_server_ready`, a cached `/health` probe that only Compose consults, it falls back at once.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import httpx
import pytest

import harbor_clerk.config as config
import harbor_clerk.llm.health as health
from harbor_clerk.llm import summarize

MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def compose_down(monkeypatch, tmp_path: Path) -> None:
    """Compose, the model's file in the volume, and nothing listening where llama-server should be."""
    models = tmp_path / "models"
    models.mkdir()
    (models / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)
    monkeypatch.setenv("LLAMA_SERVER_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(health, "_ready_probe", None)
    monkeypatch.setattr(summarize, "_apple_intelligence_summary", lambda *a, **k: None)
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-4b"  # the file is there: the model looks active


def test_a_summary_falls_back_at_once_when_the_server_is_down(compose_down, monkeypatch) -> None:
    def no_llm_call(*args, **kwargs):
        raise AssertionError("the summary called a llama-server that is not ready")

    monkeypatch.setattr(summarize, "_call_llm", no_llm_call)
    started = time.monotonic()
    text, model_used = summarize.generate_summary(["A short invoice from Acme for three widgets."])
    assert model_used == "extractive" and text
    assert time.monotonic() - started < 5


def test_a_call_that_finds_the_server_gone_does_not_retry(compose_down, monkeypatch) -> None:
    # The server went away after the readiness check: a refused connection under Compose means no
    # model is loaded, which a retry does not wait out.
    sleeps: list[float] = []
    monkeypatch.setattr(summarize.time, "sleep", sleeps.append)
    assert summarize._call_llm("system", "user", max_attempts=3, phase="test") is None
    assert sleeps == []


def test_the_probe_is_reused_until_it_expires_or_a_failure_asks_again(compose_down, monkeypatch) -> None:
    probes: list[str] = []

    def fake_get(url, **kwargs):
        probes.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", fake_get)
    assert health.llm_server_ready() is True
    assert health.llm_server_ready() is True
    assert len(probes) == 1 and probes[0].endswith("/health")
    assert health.llm_server_ready(fresh=True) is True
    assert len(probes) == 2


def test_a_server_still_loading_is_not_ready(compose_down, monkeypatch) -> None:
    monkeypatch.setattr(health.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    assert health.llm_server_ready() is False


def test_outside_compose_nothing_is_probed(monkeypatch) -> None:
    # The macOS launcher owns llama-server there, and a swap is a transient the retries wait out.
    def no_probe(*args, **kwargs):
        raise AssertionError("probed llama-server outside Compose")

    monkeypatch.setattr(health.httpx, "get", no_probe)
    monkeypatch.setattr(health, "_ready_probe", None)
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)  # set, but no HC_DEPLOYMENT marker
    config._settings = None
    assert health.llm_server_ready() is True
    assert health.llm_server_ready(fresh=True) is True
