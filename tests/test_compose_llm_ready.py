"""Under Docker Compose, summaries wait out a load or a restart, and fall back fast only from a server gone (#654).

The active model follows its file being in the volume, and a present file can still fail to load (too big
for the container, a hand copy cut short): llama-server then exits and restarts while the model looks
active, and a summary used to spend ~95 s of ConnectError retries per document against it. `llm_server_state`
(a `/health` probe only Compose consults) tells READY (200), LOADING (503, or a probe that timed out) and
UNAVAILABLE (nothing listening) apart, and `wait_for_server` decides:

- LOADING (every `compose up`, the first load after a download) is waited out;
- UNAVAILABLE for less than `RESTART_WINDOW_SECONDS` (an edited command and `up -d`, a crash, an OOM kill)
  is waited out too, or every document claimed in that gap would be stored extractive for good;
- UNAVAILABLE for longer falls back at once: only the first document after an outage waits.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import harbor_clerk.config as config
import harbor_clerk.llm.health as health
from harbor_clerk.llm import summarize

MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"
CHUNKS = ["A short invoice from Acme for three widgets."]
SUMMARY = "Acme invoices three widgets."


class Clock:
    """time.monotonic and time.sleep, with sleeping moving the clock instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        # A wait that never ends would spin forever on a clock that sleeping moves: bound it.
        if sum(self.slept) > 3600:
            raise AssertionError("waited over an hour of fake time: the wait does not end")


class Server:
    """llama-server's /health on the fake clock: nothing listening until `up_at`, then `code`."""

    def __init__(self, clock: Clock, *, up_at: float = float("inf"), code: int = 200) -> None:
        self.clock, self.up_at, self.code = clock, up_at, code
        self.probes = 0

    def __call__(self, url, **kwargs):
        self.probes += 1
        if self.clock.now < self.up_at:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
        return httpx.Response(self.code, request=httpx.Request("GET", url))


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(health.time, "monotonic", c.monotonic)
    monkeypatch.setattr(health.time, "sleep", c.sleep)  # the same module summarize sleeps through
    return c


@pytest.fixture
def compose(monkeypatch, tmp_path: Path, clock: Clock) -> None:
    """Compose, the model's file in the volume, no outage on record, Apple Intelligence absent."""
    models = tmp_path / "models"
    models.mkdir()
    (models / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://llama-server.invalid:8080")
    monkeypatch.setattr(health, "_state_probe", None)
    monkeypatch.setattr(health, "_unavailable_since", None)
    monkeypatch.setattr(summarize, "_apple_intelligence_summary", lambda *a, **k: None)
    monkeypatch.setattr(summarize, "_apple_intelligence_doc_type", lambda *a, **k: None)
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-4b"  # the file is there: the model looks active


@pytest.fixture
def llm(monkeypatch) -> list[str]:
    """The model, answering every call; records the phases it was asked for."""
    calls: list[str] = []

    def answer(system_prompt, user_content, **kwargs):
        calls.append(kwargs.get("phase", ""))
        return SUMMARY if kwargs.get("response_key") == "summary" else "Invoice"

    monkeypatch.setattr(summarize, "_call_llm", answer)
    return calls


# --- a restart: wait it out ---


def test_a_summary_claimed_during_a_restart_waits_for_the_server(compose, clock, monkeypatch, llm) -> None:
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now + 5))
    text, model_used = summarize.generate_summary(CHUNKS)
    assert (text, model_used) == (SUMMARY, "qwen35-4b"), "a summary claimed during a restart was stored extractive"
    assert 5 <= sum(clock.slept) < health.RESTART_WINDOW_SECONDS
    assert health._unavailable_since is None  # the server answering cleared the outage


def test_a_server_gone_past_the_window_costs_only_the_first_job_a_wait(compose, clock, monkeypatch, llm) -> None:
    monkeypatch.setattr(health.httpx, "get", Server(clock))  # never comes back
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "extractive" and llm == []
    first_wait = sum(clock.slept)
    assert health.RESTART_WINDOW_SECONDS <= first_wait < health.RESTART_WINDOW_SECONDS + 5
    clock.slept.clear()
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "extractive"
    assert summarize.classify_doc_type(CHUNKS, "application/pdf") == summarize._mime_to_doc_type("application/pdf")
    assert clock.slept == [], "a later job waited for a server already gone past the restart window"


def test_recovery_clears_the_outage_so_the_next_one_is_waited_out_again(compose, clock, monkeypatch, llm) -> None:
    server = Server(clock)
    monkeypatch.setattr(health.httpx, "get", server)
    summarize.generate_summary(CHUNKS)  # a long outage: on record, and fallen back from
    assert health._unavailable_since is not None
    clock.now += 3  # past the brief cache of the last verdict
    server.up_at = clock.now  # it comes back
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "qwen35-4b" and health._unavailable_since is None
    clock.now += 30
    server.up_at = clock.now + 5  # and restarts again
    clock.slept.clear()
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "qwen35-4b", "the old outage was still counted against a new restart"
    assert clock.slept


# --- the same rule inside a call ---


def _completion(content: str) -> httpx.Response:
    request = httpx.Request("POST", "http://llama-server.invalid:8080/v1/chat/completions")
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=request)


def test_a_call_cut_off_by_a_restart_retries_once_the_server_is_back(compose, clock, monkeypatch) -> None:
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now + 4))
    replies = iter([None, _completion(json.dumps({"summary": SUMMARY}))])

    def post(url, **kwargs):
        reply = next(replies)
        if reply is None:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return reply

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert summarize._call_llm("system", "user", max_attempts=3, response_key="summary", phase="test") == SUMMARY


def test_a_call_against_a_server_long_gone_does_not_retry(compose, clock, monkeypatch) -> None:
    monkeypatch.setattr(health.httpx, "get", Server(clock))
    monkeypatch.setattr(health, "_unavailable_since", clock.now - 2 * health.RESTART_WINDOW_SECONDS)
    posts: list[str] = []

    def post(url, **kwargs):
        posts.append(url)
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert summarize._call_llm("system", "user", max_attempts=3, phase="test") is None
    assert len(posts) == 1 and clock.slept == []


# --- a load: wait it out ---


def test_a_summary_claimed_during_a_load_waits_for_the_model(compose, clock, monkeypatch, llm) -> None:
    server = Server(clock, up_at=clock.now, code=503)
    monkeypatch.setattr(health.httpx, "get", server)

    def finish_loading(seconds: float) -> None:
        Clock.sleep(clock, seconds)
        if clock.now >= 1006:
            server.code = 200

    monkeypatch.setattr(health.time, "sleep", finish_loading)
    text, model_used = summarize.generate_summary(CHUNKS)
    assert (text, model_used) == (SUMMARY, "qwen35-4b"), "a summary claimed during a load was stored without the model"
    assert summarize.classify_doc_type(CHUNKS, "application/pdf") == "Invoice"


def test_a_load_longer_than_the_wait_still_goes_to_the_model(compose, clock, monkeypatch, llm) -> None:
    # Past the wait the request goes out anyway, and its own 503 retries take over: never a fast fallback.
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now, code=503))
    _, model_used = summarize.generate_summary(CHUNKS)
    assert model_used == "qwen35-4b"
    assert health.LOADING_WAIT_SECONDS <= sum(clock.slept) < health.LOADING_WAIT_SECONDS + 5


def test_a_probe_that_times_out_is_loading_not_gone(compose, monkeypatch) -> None:
    def timeout(url, **kwargs):
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", timeout)
    assert health.llm_server_state() == health.LOADING
    assert health._unavailable_since is None


# --- caching ---


def test_a_ready_verdict_is_reused_and_a_loading_one_only_briefly(compose, clock, monkeypatch) -> None:
    server = Server(clock, up_at=clock.now, code=503)
    monkeypatch.setattr(health.httpx, "get", server)
    assert health.llm_server_state() == health.LOADING
    server.code = 200
    clock.now += 2.5  # a loading verdict is not trusted past a couple of seconds
    assert health.llm_server_state() == health.READY
    assert server.probes == 2
    clock.now += 9.0
    assert health.llm_server_state() == health.READY
    assert server.probes == 2  # a ready one is reused
    assert health.llm_server_state(fresh=True) == health.READY
    assert server.probes == 3  # unless a failed request asks again


def test_outside_compose_nothing_is_probed_or_waited_for(monkeypatch, clock) -> None:
    # The macOS launcher owns llama-server there, and a swap is a transient the retries wait out.
    def no_probe(*args, **kwargs):
        raise AssertionError("probed llama-server outside Compose")

    monkeypatch.setattr(health.httpx, "get", no_probe)
    monkeypatch.setattr(health, "_state_probe", None)
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)  # set, but no HC_DEPLOYMENT marker
    config._settings = None
    assert health.llm_server_state() == health.READY
    assert health.wait_for_server(fresh=True) == health.READY
    assert clock.slept == []
