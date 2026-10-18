"""LLM server health tracking and auto-restart signaling.

Tracks consecutive 5xx failures from llama-server. After a configurable
threshold of back-to-back failures, writes ``llm_restart: true`` to
config.json so the macOS host app restarts the process.
"""

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

# How long a verdict is reused. The llm worker asks once per document. Loading and unavailable are kept
# only briefly, so a load finishing or a restart is seen at once; a refused connection answers in
# microseconds, so probing that often costs nothing.
_STATE_TTL_SECONDS = {READY: 10.0, LOADING: 2.0, UNAVAILABLE: 2.0}
_PROBE_TIMEOUT_SECONDS = 2.0
# How long a caller waits out LOADING before it sends its request anyway (and the request's own 503
# retries take over), and how often it looks again meanwhile.
LOADING_WAIT_SECONDS = 180.0
LOADING_POLL_SECONDS = 2.0
# How long nothing may listen before the server counts as gone rather than restarting. A restart (an
# edited command and `docker compose up -d`, a crash, an OOM kill) leaves a few seconds with no listener,
# the entrypoint's stability pause among them; a job claimed then waits, or every document in that window
# would be stored extractive for good. Past it, jobs fall back at once: only the first after an outage waits.
RESTART_WINDOW_SECONDS = 60.0
_state_probe: tuple[float, str] | None = None
# When the probes started finding nothing listening; None while the server answers. Per process.
_unavailable_since: float | None = None


def llm_server_state(*, fresh: bool = False) -> str:
    """Under Docker Compose, whether llama-server has its model loaded, is loading it, or is not there.

    Compose's active model follows its file being in the volume, but a present file can still fail to load
    (too big for the container, a hand copy cut short), and llama-server then exits and restarts while the
    model looks active: a summary would spend ~95 s of retries per document against it. Callers wait with
    `wait_for_server`, which waits out LOADING and a restart-length UNAVAILABLE, and fall back fast only on
    a server that has been gone longer. The verdict is reused per `_STATE_TTL_SECONDS` unless `fresh`,
    which a failed request asks for.

    Everywhere else READY, unprobed: the macOS launcher owns llama-server, and its retries are the design."""
    global _state_probe, _unavailable_since
    from harbor_clerk.config import get_settings, is_compose

    settings = get_settings()
    if not is_compose(settings):
        return READY
    now = time.monotonic()
    with _lock:
        cached = _state_probe
    if not fresh and cached is not None and now - cached[0] < _STATE_TTL_SECONDS[cached[1]]:
        return cached[1]
    try:
        code = httpx.get(f"{settings.llama_server_url}/health", timeout=_PROBE_TIMEOUT_SECONDS).status_code
        state = READY if code == 200 else LOADING if code == 503 else UNAVAILABLE
    except httpx.TimeoutException:
        state = LOADING  # it accepted the connection; a server that is up answers /health while it works
    except httpx.HTTPError:
        state = UNAVAILABLE
    with _lock:
        _state_probe = (now, state)
        if state != UNAVAILABLE:
            _unavailable_since = None
        elif _unavailable_since is None:
            _unavailable_since = now
    if cached is None or cached[1] != state:
        logger.info("llama-server at %s is %s", settings.llama_server_url, state)
    return state


def _unavailable_for(now: float) -> float:
    """Seconds the probes have found nothing listening, 0 while the server answers."""
    with _lock:
        since = _unavailable_since
    return 0.0 if since is None else now - since


def wait_for_server(*, fresh: bool = False) -> str:
    """The server's state once it is READY, or once waiting stops: LOADING past `LOADING_WAIT_SECONDS`
    of this wait, or UNAVAILABLE for `RESTART_WINDOW_SECONDS` since the outage began. A caller falls
    back on UNAVAILABLE only, so a restart is waited out and a long outage costs no wait after the first.
    READY at once outside Compose."""
    state = llm_server_state(fresh=fresh)
    loading_deadline = time.monotonic() + LOADING_WAIT_SECONDS
    while True:
        now = time.monotonic()
        loading = state == LOADING and now < loading_deadline
        restarting = state == UNAVAILABLE and _unavailable_for(now) < RESTART_WINDOW_SECONDS
        if not (loading or restarting):
            return state
        time.sleep(LOADING_POLL_SECONDS)
        state = llm_server_state(fresh=True)
