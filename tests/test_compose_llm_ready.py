"""Under Docker Compose, summaries wait out a model load and fall back fast only when the server is gone (#654).

The active model follows its file being in the volume, and a present file can still fail to load (too big
for the container, a hand copy cut short): llama-server then exits and restarts while the model looks
active, and a summary used to spend ~95 s of ConnectError retries per document against it. `llm_server_state`
(a `/health` probe only Compose consults) tells three states apart: READY (200), LOADING (503, or a probe
that timed out) and UNAVAILABLE (nothing listening). Summaries fall back at once only on UNAVAILABLE; a load
(every `compose up`, a restart, the first load after a download) is waited out, so a document claimed
during it is not stored extractive with a MIME-only type.
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
CHUNKS = ["A short invoice from Acme for three widgets."]


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def compose(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(health, "_state_probe", None)
    monkeypatch.setattr(summarize, "_apple_intelligence_summary", lambda *a, **k: None)
    monkeypatch.setattr(summarize, "_apple_intelligence_doc_type", lambda *a, **k: None)
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-4b"  # the file is there: the model looks active


class _Health:
    """A scripted /health: each probe answers the next status code (the last one repeats)."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)
        self.probes = 0

    def __call__(self, url, **kwargs):
        self.probes += 1
        code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
        return httpx.Response(code, request=httpx.Request("GET", url))


@pytest.fixture
def no_wait(monkeypatch) -> list[float]:
    """Polls happen without sleeping; returns what was slept."""
    slept: list[float] = []
    monkeypatch.setattr(health.time, "sleep", slept.append)
    return slept


# --- unavailable: fall back at once ---


def test_a_summary_falls_back_at_once_when_nothing_listens(compose, monkeypatch) -> None:
    def no_llm_call(*args, **kwargs):
        raise AssertionError("the summary called a llama-server that is not there")

    monkeypatch.setattr(summarize, "_call_llm", no_llm_call)
    started = time.monotonic()
    text, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "extractive" and text
    assert summarize.classify_doc_type(CHUNKS, "application/pdf") == summarize._mime_to_doc_type("application/pdf")
    assert time.monotonic() - started < 5


def test_a_call_that_finds_nothing_listening_does_not_retry(compose, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(summarize.time, "sleep", sleeps.append)
    assert summarize._call_llm("system", "user", max_attempts=3, phase="test") is None
    assert sleeps == []


# --- loading: wait it out ---


def test_a_summary_claimed_during_a_load_waits_for_the_model(compose, monkeypatch, no_wait) -> None:
    probe = _Health(503, 503, 503, 200)
    monkeypatch.setattr(health.httpx, "get", probe)
    calls: list[str] = []

    def llm(system_prompt, user_content, **kwargs):
        calls.append(kwargs.get("phase", ""))
        return "Acme invoices three widgets." if kwargs.get("response_key") == "summary" else "Invoice"

    monkeypatch.setattr(summarize, "_call_llm", llm)
    text, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "qwen35-4b", "a summary claimed during a load was stored without the model"
    assert text == "Acme invoices three widgets."
    assert probe.probes == 4 and len(no_wait) == 3  # polled through the load, then went to the model
    assert summarize.classify_doc_type(CHUNKS, "application/pdf") == "Invoice"


def test_a_load_longer_than_the_wait_still_goes_to_the_model(compose, monkeypatch, no_wait) -> None:
    # Past the wait the request goes out anyway, and its own 503 retries take over: never a fast fallback.
    monkeypatch.setattr(health.httpx, "get", _Health(503))
    monkeypatch.setattr(health, "LOADING_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(summarize, "_call_llm", lambda *a, **k: "Acme invoices three widgets.")
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "qwen35-4b"


def test_a_probe_that_times_out_is_loading_not_gone(compose, monkeypatch) -> None:
    def timeout(url, **kwargs):
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", timeout)
    assert health.llm_server_state() == health.LOADING


# --- caching ---


def test_a_ready_verdict_is_reused_and_a_loading_one_only_briefly(compose, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    probe = _Health(503, 200)
    monkeypatch.setattr(health.httpx, "get", probe)
    assert health.llm_server_state() == health.LOADING
    clock[0] += 2.5  # a loading verdict is not trusted past a couple of seconds
    assert health.llm_server_state() == health.READY
    assert probe.probes == 2
    clock[0] += 9.0
    assert health.llm_server_state() == health.READY
    assert probe.probes == 2  # a ready one is reused
    assert health.llm_server_state(fresh=True) == health.READY
    assert probe.probes == 3  # unless a failed request asks again


def test_outside_compose_nothing_is_probed(monkeypatch) -> None:
    # The macOS launcher owns llama-server there, and a swap is a transient the retries wait out.
    def no_probe(*args, **kwargs):
        raise AssertionError("probed llama-server outside Compose")

    monkeypatch.setattr(health.httpx, "get", no_probe)
    monkeypatch.setattr(health, "_state_probe", None)
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)  # set, but no HC_DEPLOYMENT marker
    config._settings = None
    assert health.llm_server_state() == health.READY
    assert health.wait_while_loading() == health.READY
