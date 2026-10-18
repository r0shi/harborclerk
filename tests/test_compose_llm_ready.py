"""Under Docker Compose, a document waits for llama-server within one budget, then falls back (#654).

The active model follows its file being in the volume, and a present file can still fail to load (too big
for the container, a hand copy cut short): llama-server then crash-loops through its load while the model
looks active. `llm_server_state` (a `/health` probe only Compose consults) tells READY (200), LOADING (503,
or a probe that timed out) and UNAVAILABLE (nothing listening) apart, and `wait_for_server` decides:

- each document has one `DOCUMENT_WAIT_SECONDS` budget for every wait at every call site (the checks before
  a summary and a doc type, and the retries after a refused connection or a 503); a load or a restart is
  waited out within it, and a document whose budget runs out falls back;
- a document that spends its whole budget without ever seeing READY sets a "server failing" flag: later
  documents fall back at once, after one probe each, and only a READY probe clears it (LOADING does not);
- the last attempt of a call never waits.
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
    monkeypatch.setattr(health, "_failing_since", None)
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
    assert 5 <= clock.waited() < health.DOCUMENT_WAIT_SECONDS
    assert not health.server_failing()


def test_a_load_under_the_budget_gives_a_model_summary(compose, clock, monkeypatch, llm) -> None:
    server = Server(clock, up_at=clock.now, code=503)
    monkeypatch.setattr(health.httpx, "get", server)

    def finish_loading(seconds: float) -> None:
        Clock.sleep(clock, seconds)
        if clock.now >= 1030:
            server.code = 200

    monkeypatch.setattr(health.time, "sleep", finish_loading)
    assert _document()[:2] == (SUMMARY, "qwen35-4b")


# --- a server that keeps failing: one document waits, the next ones do not ---


def test_a_crash_looping_load_costs_one_document_the_budget_and_the_rest_nothing(
    compose, clock, monkeypatch, llm
) -> None:
    server = CrashLoop(clock)
    monkeypatch.setattr(health.httpx, "get", server)
    _, model_used, doc_type = _document()
    assert (model_used, doc_type) == ("extractive", summarize._mime_to_doc_type(PDF)) and llm == []
    assert health.DOCUMENT_WAIT_SECONDS <= clock.waited() <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS
    assert health.server_failing()
    for _ in range(3):
        clock.slept.clear()
        probes = server.probes
        clock.now += 7  # documents arrive at different points of the loop, loading or not listening
        _, model_used, _ = _document()
        assert model_used == "extractive" and clock.slept == [], "a later document waited for a failing server"
        assert server.probes > probes, "a later document did not look whether the server had come back"
    assert health.server_failing()  # LOADING answers did not clear it
    server.state = lambda: 200  # the model loads at last
    assert _document()[:2] == (SUMMARY, "qwen35-4b")
    assert not health.server_failing()


def test_loading_does_not_clear_the_failing_flag(compose, clock, monkeypatch, llm) -> None:
    monkeypatch.setattr(health, "_failing_since", clock.now)
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now, code=503))
    assert _document()[1] == "extractive" and clock.slept == []
    assert health.server_failing()


def test_an_old_failure_cleared_by_ready_lets_a_later_restart_be_waited_out(compose, clock, monkeypatch, llm) -> None:
    server = Server(clock)  # gone
    monkeypatch.setattr(health.httpx, "get", server)
    assert _document()[1] == "extractive"
    assert health.server_failing()
    clock.now += 3600
    server.up_at = clock.now  # back, and a document sees it
    assert _document()[1] == "qwen35-4b"
    assert not health.server_failing()
    clock.now += 3600  # an hour later, a short restart
    server.up_at = clock.now + 5
    clock.slept.clear()
    text, model_used, _ = _document()
    assert (text, model_used) == (SUMMARY, "qwen35-4b"), "an old failure made a later restart fall back"
    assert 5 <= clock.waited() < health.DOCUMENT_WAIT_SECONDS


def test_a_server_that_recovered_unobserved_gets_its_next_restart_waited_out(compose, clock, monkeypatch, llm) -> None:
    # The review's sim_stale shape: an outage fails a document and flags the server; it comes back while no
    # document runs, so no READY probe clears the flag; much later it restarts for 5 s and a job is claimed.
    server = Server(clock)
    monkeypatch.setattr(health.httpx, "get", server)
    assert _document()[1] == "extractive"
    assert health.server_failing()
    server.up_at = clock.now + 10  # back, unobserved
    clock.now += health.FAILING_FLAG_TTL_SECONDS + 7200  # a quiet period past the flag's life
    server.up_at = clock.now + 5  # a short restart, and a job claimed in it
    clock.slept.clear()
    text, model_used, _ = _document()
    assert (text, model_used) == (SUMMARY, "qwen35-4b"), "a stale failing flag made a restart fall back"
    assert 5 <= clock.waited() < health.DOCUMENT_WAIT_SECONDS
    assert not health.server_failing()


def test_a_server_that_stays_broken_costs_one_wait_per_flag_lifetime(compose, clock, monkeypatch, llm) -> None:
    # A document a minute against a crash loop for most of an hour: one document per flag lifetime waits
    # its budget (and re-flags the server), every other one falls back at once.
    monkeypatch.setattr(health.httpx, "get", CrashLoop(clock))
    waited_at: list[float] = []
    for _ in range(50):
        clock.slept.clear()
        started = clock.now
        assert _document()[1] == "extractive"
        if clock.slept:
            assert health.DOCUMENT_WAIT_SECONDS <= clock.waited() <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS
            waited_at.append(started)
        clock.now += 60
    assert len(waited_at) == 4, f"documents that waited: {len(waited_at)} of 50"
    gaps = [b - a for a, b in zip(waited_at, waited_at[1:], strict=False)]
    ttl = health.FAILING_FLAG_TTL_SECONDS
    assert all(ttl <= gap <= ttl + 60 + health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS for gap in gaps), gaps


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
    assert clock.now - started <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS < STAGE_TIMEOUT_SECONDS


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


# --- a slow load is not a crash loop ---


def test_a_slow_load_with_a_queue_of_documents_costs_at_most_the_first_one(compose, clock, monkeypatch, llm) -> None:
    # 150 s of loading (503 throughout, nothing ever refused) with 80 documents queued a second apart. The
    # first document waits its 90 s and falls back; it saw only LOADING, so it flags nothing, and the second
    # waits out the rest of the load. Flagging it made 59 of 80 summaries extractive for good.
    t0 = clock.now
    server = Server(clock, up_at=t0, code=503)
    monkeypatch.setattr(health.httpx, "get", server)

    def finish_loading(seconds: float) -> None:
        Clock.sleep(clock, seconds)
        if clock.now >= t0 + 150:
            server.code = 200

    monkeypatch.setattr(health.time, "sleep", finish_loading)
    used = []
    for i in range(80):
        clock.now = max(clock.now, t0 + i)  # the worker takes the next queued document
        used.append(_document()[1])
        assert not health.server_failing(), f"a slow load flagged the server at document {i}"
    assert used.count("extractive") <= 1, f"{used.count('extractive')} of 80 summaries stored extractive"
    assert used[1:] == ["qwen35-4b"] * 79


def test_a_load_that_never_finishes_costs_each_document_its_own_budget(compose, clock, monkeypatch, llm) -> None:
    # Accepted: a wedged loader is rare, and it is bounded per document. Each one waits its 90 s and falls
    # back; none flags the server, because it never stopped answering.
    monkeypatch.setattr(health.httpx, "get", Server(clock, up_at=clock.now, code=503))
    for _ in range(3):
        clock.slept.clear()
        assert _document()[1] == "extractive"
        assert health.DOCUMENT_WAIT_SECONDS <= clock.waited() <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS
        assert not health.server_failing()


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
    monkeypatch.setattr(health, "_failing_since", None)
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
    back through 3 s with nothing listening and 27 s of loading. It is READY again and again, so the
    failing flag never trips; only the document's budget bounds the waiting."""

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
    assert clock.now - started <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS < STAGE_TIMEOUT_SECONDS


def test_a_document_that_saw_the_server_ready_does_not_flag_it(compose, clock, monkeypatch) -> None:
    # Ready when the document starts, gone for the rest of it: the server was not failing to load, so the
    # next document still waits for it, and gets the model when it is back.
    server = Server(clock, up_at=clock.now)
    monkeypatch.setattr(health.httpx, "get", server)
    gone = [True]

    def post(url, **kwargs):
        if gone[0]:
            server.up_at = float("inf")
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return _completion(json.dumps({"summary": SUMMARY}))

    monkeypatch.setattr(summarize.httpx, "post", post)
    assert _document()[1] == "extractive"
    assert not health.server_failing()
    clock.now += 600
    server.up_at = clock.now + 5  # back after a short restart
    gone[0] = False
    clock.slept.clear()
    text, model_used, _ = _document()
    assert (text, model_used) == (SUMMARY, "qwen35-4b")
    assert 5 <= clock.waited() < health.DOCUMENT_WAIT_SECONDS


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


@pytest.mark.parametrize("after", ["crash-loop", "gone"])
def test_after_long_work_a_failing_server_costs_one_budget_of_waiting(compose, clock, monkeypatch, after) -> None:
    # Ready for the first 150 s of a long document (calls take 30 s each), then the server crash-loops or is
    # gone for good. That document waits at most one budget in all, however long it has run; the next one,
    # which never sees it ready, waits one budget and flags it; the ones after fall back at once.
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
    assert clock.now - started > health.DOCUMENT_WAIT_SECONDS + INFERENCE_SECONDS  # long in elapsed time
    assert clock.waited() <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS, "more than one budget waited"
    assert not health.server_failing()  # it saw the server ready
    clock.slept.clear()
    assert _document()[1] == "extractive"
    assert health.DOCUMENT_WAIT_SECONDS <= clock.waited() <= health.DOCUMENT_WAIT_SECONDS + health.POLL_SECONDS
    assert health.server_failing()
    for _ in range(3):
        clock.slept.clear()
        clock.now += 5
        assert _document()[1] == "extractive" and clock.slept == []
