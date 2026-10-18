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


# How long a readiness probe of llama-server is reused. The llm worker asks once per document, and a
# server that is down answers at once (connection refused), so this bounds requests, not waiting.
_READY_TTL_SECONDS = 10.0
_READY_TIMEOUT_SECONDS = 2.0
_ready_probe: tuple[float, bool] | None = None


def llm_server_ready(*, fresh: bool = False) -> bool:
    """Under Docker Compose, whether llama-server has its model loaded: `/health` answers 200 (#654).

    Compose's active model follows its file being in the volume, but a file that is present can still
    fail to load (too big for the container, a hand copy cut short), and llama-server then exits and
    restarts while the model looks active. A summary would spend ~95 s of retries on each document
    against it; with this it falls back at once. The probe is reused for `_READY_TTL_SECONDS` unless
    `fresh`, which a failed request asks for.

    Everywhere else True, unprobed: the macOS launcher owns llama-server, and a model swap there is a
    transient the summarize retries are built to wait out."""
    global _ready_probe
    from harbor_clerk.config import get_settings, is_compose

    settings = get_settings()
    if not is_compose(settings):
        return True
    now = time.monotonic()
    with _lock:
        cached = _ready_probe
    if not fresh and cached is not None and now - cached[0] < _READY_TTL_SECONDS:
        return cached[1]
    try:
        ready = httpx.get(f"{settings.llama_server_url}/health", timeout=_READY_TIMEOUT_SECONDS).status_code == 200
    except httpx.HTTPError:
        ready = False
    with _lock:
        _ready_probe = (now, ready)
    if cached is None or cached[1] != ready:
        logger.info("llama-server at %s is %s", settings.llama_server_url, "ready" if ready else "not ready")
    return ready
