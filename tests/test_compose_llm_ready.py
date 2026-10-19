"""Under Docker Compose, a document waits for llama-server within one budget of waiting, then falls back (#654).

The active model follows its file being in the volume, and a present file can still fail to load (too big
for the container, a hand copy cut short). `llm_server_state` (a `/health` probe only Compose consults) tells
READY (200), LOADING (503, or a read timeout) and UNAVAILABLE (nothing listening, or a connect timeout) apart,
and `wait_for_server` waits until READY or until the document's `DOCUMENT_WAIT_SECONDS` of waiting are spent:

- every wait at every call site (the checks before a summary and a doc type, and the retries after a refused
  connection or a 503) draws on the one budget, which only waiting is charged with, never inference or yields;
- a load or a restart is waited out within it; a document whose budget runs out falls back;
- there is no memory across documents: in a crash loop or a long outage each document waits its own budget
  (accepted: bounded per document, far inside the stage timeout, and summarize gates nothing);
- the last attempt of a call never waits; a 503 while /health says ready waits the jittered 5-15 s.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

import harbor_clerk.config as config
import harbor_clerk.llm.health as health
from harbor_clerk.llm import summarize

MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"
CHUNKS = ["A short invoice from Acme for three widgets."]
SUMMARY = "Acme invoices three widgets."
PDF = "application/pdf"
STAGE_TIMEOUT_SECONDS = 900  # worker/pipeline.py: summarize's
INFERENCE_SECONDS = 30.0  # one LLM call on a CPU-only container
YIELD_SECONDS = 10.0  # one idle yield between map calls
# The decided per-document budget of waiting, stated here rather than read from health, so a changed budget
# fails these tests instead of moving them with it.
BUDGET_SECONDS = 90.0


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
        if sum(self.slept) > 3600 or len(self.slept) > 10_000:
            raise AssertionError("waited over an hour of fake time, or slept 10,000 times: the wait does not end")

    def waited(self) -> float:
        return sum(self.slept)


class Server:
    """llama-server on the fake clock: nothing listening until `up_at`, then `code` (200, or 503 loading)."""

    def __init__(self, clock: Clock, *, up_at: float = float("inf"), code: int = 200) -> None:
        self.clock, self.up_at, self.code = clock, up_at, code
        self.probes = 0

    def state(self) -> int | None:
        return None if self.clock.now < self.up_at else self.code

    def __call__(self, url, **kwargs):
        self.probes += 1
        code = self.state()
        if code is None:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
        return httpx.Response(code, request=httpx.Request("GET", url))


class CrashLoop(Server):
    """A model too big for the container: about 3 s with nothing listening, then 20 s loading, then it
    dies and the cycle repeats. It is never READY."""

    def state(self) -> int | None:
        return None if (self.clock.now - 1000.0) % 23.0 < 3.0 else 503


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(health.time, "monotonic", c.monotonic)
    monkeypatch.setattr(health.time, "sleep", c.sleep)  # the same module summarize sleeps through
    return c


@pytest.fixture
def compose(monkeypatch, tmp_path: Path, clock: Clock) -> None:
    """Compose, the model's file in the volume, the server not flagged, Apple Intelligence absent."""
    models = tmp_path / "models"
    models.mkdir()
    (models / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    monkeypatch.setenv("LLAMA_SERVER_URL", "http://llama-server.invalid:8080")
    monkeypatch.setattr(health, "_state_probe", None)
    monkeypatch.setattr(summarize, "_apple_intelligence_summary", lambda *a, **k: None)
    monkeypatch.setattr(summarize, "_apple_intelligence_doc_type", lambda *a, **k: None)
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-4b"  # the file is there: the model looks active


@pytest.fixture
def llm(monkeypatch, clock: Clock) -> list[str]:
    """The model, answering every call after INFERENCE_SECONDS of fake time; records the phases asked for.
    Inference takes time so that no test can pass on a budget that elapsed time, not waiting, would spend."""
    calls: list[str] = []

    def answer(system_prompt, user_content, **kwargs):
        calls.append(kwargs.get("phase", ""))
        clock.now += INFERENCE_SECONDS
        return SUMMARY if kwargs.get("response_key") == "summary" else "Invoice"

    monkeypatch.setattr(summarize, "_call_llm", answer)
    return calls


def _document(chunks=CHUNKS) -> tuple[str, str, str]:
    """One document as the summarize stage handles it: summary and type under one wait budget."""
    with health.document_wait_budget():
        text, model_used = summarize.generate_summary(chunks)
        doc_type = summarize.classify_doc_type(chunks, PDF)
    return text, model_used, doc_type


# --- a restart or a load: waited out ---


def test_a_restart_under_the_budget_gives_a_model_summary(compose, clock, monkeypatch, llm) -> None:
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now + 5))
    text, model_used, doc_type = _document()
    assert (text, model_used, doc_type) == (SUMMARY, "qwen35-4b", "Invoice")
    assert 5 <= clock.waited() < BUDGET_SECONDS


def test_a_load_under_the_budget_gives_a_model_summary(compose, clock, monkeypatch, llm) -> None:
    server = Server(clock, up_at=clock.now, code=503)
    monkeypatch.setattr(health.httpx, "get", server)

    def finish_loading(seconds: float) -> None:
        Clock.sleep(clock, seconds)
        if clock.now >= 1030:
            server.code = 200

    monkeypatch.setattr(health.time, "sleep", finish_loading)
    assert _document()[:2] == (SUMMARY, "qwen35-4b")


# --- inside a call ---


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
    assert 4 <= clock.waited() < 10  # waited for the server, not the fixed retry delay


@pytest.mark.parametrize("failure", ["refused", "loading"])
def test_the_last_attempt_never_waits(compose, clock, monkeypatch, failure) -> None:
    server = Server(clock, up_at=clock.now + 5)
    monkeypatch.setattr(health.httpx, "get", server)

    def post(url, **kwargs):
        if failure == "refused":
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert summarize._call_llm("system", "user", max_attempts=1, phase="doc-type") is None
    assert clock.slept == [] and server.probes == 0


def test_a_long_document_against_a_failing_server_stays_inside_the_stage_timeout(compose, clock, monkeypatch) -> None:
    # Ready when the document starts, then crash-looping for every map and reduce call: each call's waits
    # draw on the document's one budget, so 400 chunks cost the budget once, not once per call.
    loop = CrashLoop(clock)
    ready_once = iter([True])

    def get(url, **kwargs):
        if next(ready_once, False):
            return httpx.Response(200, request=httpx.Request("GET", url))
        return loop(url, **kwargs)

    def post(url, **kwargs):
        code = loop.state()
        if code is None:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return httpx.Response(code, request=httpx.Request("POST", url))

    monkeypatch.setattr(health.httpx, "get", get)
    monkeypatch.setattr(summarize.httpx, "post", post)
    chunks = ["A long contract clause about termination, notice and the obligations that survive it. " * 12] * 400
    started = clock.now
    _, model_used, _ = _document(chunks)
    assert model_used == "extractive"
    assert clock.now - started <= BUDGET_SECONDS + health.POLL_SECONDS < STAGE_TIMEOUT_SECONDS


# --- the probe ---


def test_a_probe_that_times_out_is_loading_not_gone(compose, monkeypatch) -> None:
    def timeout(url, **kwargs):
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", timeout)
    assert health.llm_server_state() == health.LOADING


def test_a_probe_whose_connection_timed_out_is_gone(compose, monkeypatch) -> None:
    # Nothing accepted the connection: that is not a server slow to answer.
    def timeout(url, **kwargs):
        raise httpx.ConnectTimeout("no answer", request=httpx.Request("GET", url))

    monkeypatch.setattr(health.httpx, "get", timeout)
    assert health.llm_server_state() == health.UNAVAILABLE


def test_a_503_from_a_ready_server_retries_after_the_jittered_delay(compose, clock, monkeypatch) -> None:
    # /health says ready, the completion answers 503: a busy server, not a load. The retries are spaced by
    # the old 5-15 s jittered delay, not fired in the same instant; the last attempt does not wait.
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now))
    posted_at: list[float] = []

    def post(url, **kwargs):
        posted_at.append(clock.now)
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert summarize._call_llm("system", "user", max_attempts=3, phase="test") is None
    gaps = [b - a for a, b in zip(posted_at, posted_at[1:], strict=False)]
    assert len(posted_at) == 3 and all(5 <= gap <= 5 + 10 * 3 for gap in gaps), gaps
    assert clock.now == posted_at[-1], "the last attempt waited"


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
    assert server.probes == 3


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


# --- the stage ---


def test_the_stage_holds_one_budget_for_the_summary_and_the_type(monkeypatch) -> None:

    from harbor_clerk.worker.stages import summarize as stage

    seen: list[object] = []

    def record(*args, **kwargs):
        seen.append(health._document_budget.get())
        return ("text", "model") if len(seen) == 1 else "Invoice"

    monkeypatch.setattr(stage, "generate_summary", record)
    monkeypatch.setattr(stage, "classify_doc_type", record)
    assert stage._summary_and_type(uuid.uuid4(), CHUNKS, PDF) == ("text", "model", "Invoice")
    assert seen[0] is not None and seen[0] is seen[1], "the summary and the type waited on separate budgets"


class OomOnPrompt(Server):
    """A model that loads but dies on its first prompt: READY, then every request kills it, and it comes
    back through 3 s with nothing listening and 27 s of loading. It is READY again and again, so every
    call site's wait succeeds; only the document's one budget bounds the waiting."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.crashed_at: float | None = None

    def state(self) -> int | None:
        if self.crashed_at is None:
            return 200
        since = self.clock.now - self.crashed_at
        return None if since < 3 else 503 if since < 30 else 200

    def post(self, url, **kwargs):
        code = self.state()
        if code == 200:
            self.crashed_at = self.clock.now  # the prompt kills it
        if code in (200, None):
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return httpx.Response(503, request=httpx.Request("POST", url))


def test_a_server_that_dies_on_every_prompt_costs_a_long_document_one_budget(compose, clock, monkeypatch) -> None:
    # Every call site waits for READY and gets it, then the request kills the server again: with a budget
    # per call site a 400-chunk document would wait 30 s per call, far past the stage timeout.
    server = OomOnPrompt(clock)
    monkeypatch.setattr(health.httpx, "get", server)
    monkeypatch.setattr(summarize.httpx, "post", server.post)
    chunks = ["A long contract clause about termination, notice and the obligations that survive it. " * 12] * 400
    started = clock.now
    _, model_used, _ = _document(chunks)
    assert model_used == "extractive"
    assert clock.now - started <= BUDGET_SECONDS + health.POLL_SECONDS < STAGE_TIMEOUT_SECONDS


def test_generate_summary_alone_holds_one_budget_across_its_calls(compose, clock, monkeypatch) -> None:
    # Called bare (no stage, no document_wait_budget around it), generate_summary still makes its map and reduce
    # calls share one budget: against a server that dies on every prompt, one budget of waiting in all.
    server = OomOnPrompt(clock)
    monkeypatch.setattr(health.httpx, "get", server)
    monkeypatch.setattr(summarize.httpx, "post", server.post)
    chunks = ["A long contract clause about termination, notice and the obligations that survive it. " * 12] * 400
    _, model_used = summarize.generate_summary(chunks)
    assert model_used == "extractive"
    assert clock.waited() <= BUDGET_SECONDS + health.POLL_SECONDS, "a bare generate_summary waited more than one budget"


def test_the_stage_still_classifies_when_the_summary_fails(monkeypatch) -> None:
    # A summary that raises is logged and dropped: no summary is stored, and the type is still classified.
    from harbor_clerk.worker.stages import summarize as stage

    def failing_summary(*args, **kwargs):
        raise RuntimeError("summary blew up")

    monkeypatch.setattr(stage, "generate_summary", failing_summary)
    monkeypatch.setattr(stage, "classify_doc_type", lambda *a, **k: "Invoice")
    assert stage._summary_and_type(uuid.uuid4(), CHUNKS, PDF) == (None, None, "Invoice")


def test_float_residue_in_the_budget_does_not_keep_a_wait_spinning(compose, clock, monkeypatch) -> None:
    # The review's case: a connection cut mid-request (RemoteProtocolError) takes the jittered retry delay (seed 1),
    # which leaves the clock at a fraction; every later 2 s charge is then (t + 2) - t, a hair under 2, and the
    # budget ends at about 1e-14 s left. Waiting on that charged nothing and spun; less than 10 ms left is spent.
    import random

    random.seed(1)
    monkeypatch.setattr(health.httpx, "get", Server(clock))  # nothing listening, ever
    replies = iter([httpx.RemoteProtocolError("peer closed connection")])

    def post(url, **kwargs):
        exc = next(replies, None) or httpx.ConnectError("refused")
        raise exc

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert summarize._call_llm("system", "user", max_attempts=3, phase="test") is None
    assert len(clock.slept) < 100, f"{len(clock.slept)} sleeps: the wait spun on float residue"
    assert clock.waited() <= BUDGET_SECONDS + 5 + 10 + health.POLL_SECONDS  # the budget plus one jittered delay


# --- the budget is waiting time, not elapsed time ---

LONG_CHUNKS = ["A long contract clause about termination, notice and the obligations that survive it. " * 12] * 120


def _is_reduce(payload: dict) -> bool:
    """The long tier's reduce call: its user message is the numbered section summaries."""
    return payload["messages"][-1]["content"].startswith("1. ")


def test_a_restart_late_in_a_long_document_is_waited_out_through_the_stage(compose, clock, monkeypatch) -> None:
    # A map-reduce document on a CPU-only container: every call takes 30 s and every map call yields 10 s,
    # so it is well past the 90 s budget in elapsed time when llama-server restarts under its reduce call,
    # and again as its type is asked for. Neither restart may find the budget spent: only waiting counts.
    from harbor_clerk.worker.stages import summarize as stage

    server = Server(clock, up_at=clock.now)
    monkeypatch.setattr(health.httpx, "get", server)
    started = clock.now
    reduce_restarted_at: list[float] = []

    def post(url, **kwargs):
        if server.state() is None:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        if _is_reduce(kwargs["json"]) and not reduce_restarted_at:
            reduce_restarted_at.append(clock.now)
            server.up_at = clock.now + 5  # the restart takes the request down with it
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        clock.now += INFERENCE_SECONDS
        return _completion(json.dumps({"summary": SUMMARY, "document_type": "Invoice"}))

    def idle_yield(*args, **kwargs):
        clock.now += YIELD_SECONDS

    real_classify = stage.classify_doc_type

    def classify_during_a_restart(*args, **kwargs):
        server.up_at = clock.now + 5  # restarts again just as the type is asked for
        return real_classify(*args, **kwargs)

    monkeypatch.setattr(summarize.httpx, "post", post)
    monkeypatch.setattr(stage, "_wait_for_idle_llm", idle_yield)
    monkeypatch.setattr(stage, "classify_doc_type", classify_during_a_restart)
    summary, model_used, doc_type = stage._summary_and_type(uuid.uuid4(), LONG_CHUNKS, PDF)
    assert reduce_restarted_at and reduce_restarted_at[0] - started > 200, "the document was not long yet"
    assert (summary, model_used) == (SUMMARY, "qwen35-4b"), "a restart late in a long document lost the summary"
    assert doc_type == "Invoice", "the type after a long summary got no wait budget"
    assert 10 <= clock.waited() < 2 * 5 + 2 * health.POLL_SECONDS + 1


# --- a load, a crash loop, an outage: each document waits at most its own budget ---


class GapThenLoad(Server):
    """A real first load (after a download, or a restart): nothing listening for `gap` seconds (the entrypoint's
    poll, its stability check, the process starting), then 503 for `load` seconds, then READY."""

    def __init__(self, clock: Clock, *, gap: float, load: float) -> None:
        super().__init__(clock)
        self.t0, self.gap, self.load = clock.now, gap, load

    def state(self) -> int | None:
        since = self.clock.now - self.t0
        return None if since < self.gap else 503 if since < self.gap + self.load else 200


def test_a_startup_gap_and_a_slow_load_cost_at_most_the_first_of_80_documents(compose, clock, monkeypatch, llm) -> None:
    # 12 s with nothing listening, then 150 s of loading, with 80 documents queued a second apart. The first
    # waits its 90 s and falls back; the second waits out the rest of the load; the other 78 find it ready. A
    # memory of "it failed last time" made every one of the 80 extractive (#654, seventh review).
    t0 = clock.now
    monkeypatch.setattr(health.httpx, "get", GapThenLoad(clock, gap=12, load=150))
    used = []
    for i in range(80):
        clock.now = max(clock.now, t0 + i)  # the worker takes the next queued document
        used.append(_document()[1])
    assert used.count("extractive") <= 1, f"{used.count('extractive')} of 80 summaries stored extractive"
    assert used[1:] == ["qwen35-4b"] * 79


@pytest.mark.parametrize("server", ["crash-loop", "gone", "never-loads"])
def test_a_failing_server_costs_each_document_at_most_its_own_budget(compose, clock, monkeypatch, llm, server) -> None:
    # A crash loop, a long outage, a load that never finishes: each document waits its 90 s of waiting and falls
    # back, and none waits more. Accepted: bounded per document, far inside the stage's timeout.
    servers = {
        "crash-loop": CrashLoop(clock),
        "gone": Server(clock),
        "never-loads": Server(clock, up_at=clock.now, code=503),
    }
    monkeypatch.setattr(health.httpx, "get", servers[server])
    for _ in range(4):
        clock.slept.clear()
        clock.now += 7  # documents arrive at different points of the loop
        _, model_used, doc_type = _document()
        assert (model_used, doc_type) == ("extractive", summarize._mime_to_doc_type(PDF))
        assert BUDGET_SECONDS <= clock.waited() <= BUDGET_SECONDS + health.POLL_SECONDS


@pytest.mark.parametrize("after", ["crash-loop", "gone"])
def test_after_long_work_a_failing_server_costs_one_budget_of_waiting(compose, clock, monkeypatch, after) -> None:
    # Ready for the first 150 s of a long document (calls take 30 s each), then the server crash-loops or is
    # gone for good: that document waits at most one budget in all, however long it has run.
    switch_at = clock.now + 150
    loop = CrashLoop(clock)

    def code() -> int | None:
        if clock.now < switch_at:
            return 200
        return loop.state() if after == "crash-loop" else None

    def get(url, **kwargs):
        c = code()
        if c is None:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
        return httpx.Response(c, request=httpx.Request("GET", url))

    def post(url, **kwargs):
        c = code()
        if c is None:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        if c == 503:
            return httpx.Response(503, request=httpx.Request("POST", url))
        clock.now += INFERENCE_SECONDS
        return _completion(json.dumps({"summary": SUMMARY, "document_type": "Invoice"}))

    monkeypatch.setattr(health.httpx, "get", get)
    monkeypatch.setattr(summarize.httpx, "post", post)
    started = clock.now
    _document(LONG_CHUNKS)
    assert clock.now - started > BUDGET_SECONDS + INFERENCE_SECONDS  # long in elapsed time
    assert clock.waited() <= BUDGET_SECONDS + health.POLL_SECONDS, "more than one budget waited"


def test_a_lone_doc_type_call_gets_a_budget_of_its_own(compose, clock, monkeypatch) -> None:
    # classify_doc_type outside the stage: its one wait (the check before its single attempt) takes a budget
    # of its own in wait_for_server, so it needs no wrapper of its own.
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now + 5))
    monkeypatch.setattr(summarize, "_call_llm", lambda *a, **k: "Invoice")
    assert summarize.classify_doc_type(CHUNKS, PDF) == "Invoice"
    assert 5 <= clock.waited() < BUDGET_SECONDS
