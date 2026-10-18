"""LLM server health tracking and auto-restart signaling.

Tracks consecutive 5xx failures from llama-server. After a configurable
threshold of back-to-back failures, writes ``llm_restart: true`` to
config.json so the macOS host app restarts the process.
"""

import contextlib
import contextvars
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_RESTART_THRESHOLD = 2
_MIN_RESTART_INTERVAL = 30.0  # don't signal more than once per 30s

_lock = threading.Lock()
_consecutive_5xx = 0
_last_restart_signal: float = 0.0


def report_llm_success() -> None:
    """Call after a successful LLM response to reset the failure counter."""
    global _consecutive_5xx
    with _lock:
        _consecutive_5xx = 0


def report_llm_error(status_code: int) -> None:
    """Call after an LLM HTTP error. Triggers restart signal if threshold is met."""
    global _consecutive_5xx, _last_restart_signal

    if status_code < 500:
        return

    with _lock:
        _consecutive_5xx += 1
        count = _consecutive_5xx
        now = time.monotonic()
        since_last = now - _last_restart_signal

        if count >= _RESTART_THRESHOLD and since_last >= _MIN_RESTART_INTERVAL:
            _last_restart_signal = now
            _consecutive_5xx = 0
            logger.warning(
                "LLM server returned %d %d times in a row — signaling restart",
                status_code,
                count,
            )
            _signal_restart()
        else:
            logger.info(
                "LLM 5xx #%d (threshold %d, cooldown %.0fs remaining)",
                count,
                _RESTART_THRESHOLD,
                max(0, _MIN_RESTART_INTERVAL - since_last),
            )


def _signal_restart() -> None:
    """Write llm_restart flag to config.json for the Swift host app."""
    from harbor_clerk.config import sync_native_config

    try:
        sync_native_config("llm_restart", True)
        logger.info("Wrote llm_restart signal to config.json")
    except Exception:
        logger.exception("Failed to write llm_restart signal")


def request_llm_restart(reason: str) -> None:
    """Public API: explicitly request a llama-server restart.

    Used when the API performs an action that requires llama-server to pick
    up a new configuration (model switch, YaRN toggle), regardless of the
    5xx error counter. Without this, the Swift host's config-watcher path
    is unreliable for distinguishing a model_id change from other settings,
    so the model switch silently does nothing. See the
    project_llm_restart_investigation memory note for the diagnosis trail.
    """
    global _last_restart_signal, _consecutive_5xx
    with _lock:
        _last_restart_signal = time.monotonic()
        _consecutive_5xx = 0
    logger.info("Explicit llama-server restart requested: %s", reason)
    _signal_restart()


# What llama-server says about its model under Docker Compose (#654), from its `/health`.
READY = "ready"  # 200: the model is loaded
LOADING = "loading"  # 503 "Loading model", or a probe that timed out: it is up, and the model is coming
UNAVAILABLE = "unavailable"  # nothing listening: restarting, waiting for its file, or gone after a failed load

# How long a verdict is reused. A loading or unavailable one is kept only briefly, so a load finishing or a
# restart is seen at once; a refused connection answers in microseconds, so probing that often costs nothing.
_STATE_TTL_SECONDS = {READY: 10.0, LOADING: 2.0, UNAVAILABLE: 2.0}
_PROBE_TIMEOUT_SECONDS = 2.0
# How often a wait looks again.
POLL_SECONDS = 2.0
# What one document may spend, in all, waiting for the server to be ready: every wait at every call site
# (the checks before a summary or a doc type, and the retries after a refused connection or a 503) draws on
# it. A load or a restart (an edited command and `up -d`, a crash) is waited out within it; a document
# whose budget runs out falls back. Only waiting is charged, never inference or idle yields. Bounded per
# document, so a server that keeps failing (a model too big, crash-looping through its load) adds at most
# this much to a document, far inside the stage's 900 s timeout. There is deliberately no memory across
# documents: a real load nearly always starts with seconds of nothing listening (the entrypoint's poll and
# stability check, a restart), so "it failed for the last document" cannot tell a crash loop from a slow
# load, and treating a load as a failure stored every document claimed during it extractive (#654 review).
DOCUMENT_WAIT_SECONDS = 90.0
# Less than this left in a budget is spent: float residue (90 minus a run of 2 s charges) must not keep a wait
# going on a sleep of 1e-14 s that a clock cannot even register.
_BUDGET_EPSILON_SECONDS = 0.01
_state_probe: tuple[float, str] | None = None


def llm_server_state(*, fresh: bool = False) -> str:
    """Under Docker Compose, whether llama-server has its model loaded, is loading it, or is not there.

    Compose's active model follows its file being in the volume, but a present file can still fail to load
    (too big for the container, a hand copy cut short), and llama-server then exits and restarts while the
    model looks active. Callers wait through `wait_for_server`, within the document's budget. The verdict is
    reused per `_STATE_TTL_SECONDS` unless `fresh`.

    Everywhere else READY, unprobed: the macOS launcher owns llama-server, and its retries are the design."""
    global _state_probe
    from harbor_clerk.config import get_settings

    settings = get_settings()
    if not server_is_probed():
        return READY
    now = time.monotonic()
    with _lock:
        cached = _state_probe
    if not fresh and cached is not None and now - cached[0] < _STATE_TTL_SECONDS[cached[1]]:
        return cached[1]
    try:
        code = httpx.get(f"{settings.llama_server_url}/health", timeout=_PROBE_TIMEOUT_SECONDS).status_code
        state = READY if code == 200 else LOADING if code == 503 else UNAVAILABLE
    except httpx.ConnectTimeout:
        state = UNAVAILABLE  # nothing accepted the connection
    except httpx.TimeoutException:
        state = LOADING  # it accepted the connection but was slow to answer; a server that is up answers /health
    except httpx.HTTPError:
        state = UNAVAILABLE
    with _lock:
        _state_probe = (now, state)
    if cached is None or cached[1] != state:
        logger.info("llama-server at %s is %s", settings.llama_server_url, state)
    return state


def server_is_probed() -> bool:
    """Whether this process asks llama-server for its state: only a Docker Compose service does."""
    from harbor_clerk.config import get_settings, is_compose

    return is_compose(get_settings())


class WaitBudget:
    """The time one document may still spend waiting for the server.

    Charged only with time spent waiting (`wait_for_server`'s sleeps and probes), never with elapsed time:
    inference and the long tier's idle yields can take minutes on a CPU-only container, and a restart late
    in such a document must still be waited out, as the summarize retries would have."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.spent = 0.0

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.spent)

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)


_document_budget: contextvars.ContextVar[WaitBudget | None] = contextvars.ContextVar(
    "document_wait_budget", default=None
)


@contextlib.contextmanager
def document_wait_budget():
    """One `DOCUMENT_WAIT_SECONDS` budget for every wait while one document is summarized and classified.

    Re-entrant: inside a budget, this reuses it, so the summarize stage can hold one across both calls and
    each call still gets its own when called alone."""
    budget = _document_budget.get()
    if budget is not None:
        yield budget
        return
    budget = WaitBudget(DOCUMENT_WAIT_SECONDS)
    token = _document_budget.set(budget)
    try:
        yield budget
    finally:
        _document_budget.reset(token)


def wait_for_server(*, fresh: bool = False) -> str:
    """The server's state once it is READY, or once the document's wait budget is spent (then not READY).

    Only the time spent in this loop is charged to the budget. Outside a `document_wait_budget` the call gets a
    budget of its own. READY at once, unprobed, outside Compose."""
    state = llm_server_state(fresh=fresh)
    budget = _document_budget.get() or WaitBudget(DOCUMENT_WAIT_SECONDS)
    while state != READY and budget.remaining() > _BUDGET_EPSILON_SECONDS:
        started = time.monotonic()
        time.sleep(min(POLL_SECONDS, budget.remaining()))
        state = llm_server_state(fresh=True)
        budget.charge(time.monotonic() - started)
    return state


# What Ask and Research say under Docker Compose when llama-server refuses the connection or answers 503
# (#654). The model counts as active as soon as its file is in the volume; llama-server starts a few seconds
# later and then loads, and in that gap the raw "not running, activate a model" advice would be wrong.
MODEL_STARTING_MESSAGE = (
    "The local model is starting; try again in a moment. If this persists, check `docker compose logs llama-server`."
)
# The same requests under Compose when llama-server accepted them and then stopped answering (a read timeout, a
# connection cut mid-answer). Activating a model, the macOS advice, is refused under Compose; the cause there is
# a slow prompt on a CPU-only container or a model too big for its memory.
MODEL_STOPPED_MESSAGE = (
    "The local model stopped answering or took too long; check `docker compose logs llama-server` "
    "(it may be overloaded, or need more memory than the container has)."
)


def server_unready_message(default: str, *, status_code: int | None = None, exc: BaseException | None = None) -> str:
    """The user-facing text for a chat or research request llama-server did not serve.

    Under Compose, a refused or unanswered connection (`httpx.ConnectError`, `httpx.ConnectTimeout`) or a 503 means it is
    starting or loading its model: `MODEL_STARTING_MESSAGE`. Any other transport error means it accepted the
    request and stopped answering: `MODEL_STOPPED_MESSAGE`. Other HTTP errors, and everything outside Compose,
    keep `default`."""
    if not server_is_probed():
        return default
    if status_code == 503 or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return MODEL_STARTING_MESSAGE
    if isinstance(exc, httpx.TransportError):
        return MODEL_STOPPED_MESSAGE
    return default
