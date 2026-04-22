"""LLM server health tracking and auto-restart signaling.

Tracks consecutive 5xx failures from llama-server. After a configurable
threshold of back-to-back failures, writes ``llm_restart: true`` to
config.json so the macOS host app restarts the process.
"""

import logging
import threading
import time

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
