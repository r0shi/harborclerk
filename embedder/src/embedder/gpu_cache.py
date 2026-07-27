"""Return GPU allocator cache to the OS when it grows past a high-water mark.

PyTorch's caching allocator keeps freed device blocks for reuse and never
returns them. That is normally self-limiting — a steady workload reaches a
steady state and the cache is pure win. It is not self-limiting here, because
the inputs are documents and email bodies whose lengths vary by orders of
magnitude: nearly every batch asks for a shape the cache has not seen, so it
allocates a fresh block and the cache ratchets upward forever.

Measured on the Mac mini (M4, 32 GB) during a 7,449-message email ingest:

    fresh embedder process          1.5 GB
    after ~1.5 hours of ingest       40 GB   (phys_footprint, peak 44 GB)

At that point the machine had 347 MB free, 15 GB in the compressor and 13 GB of
swap in use. Restarting the embedder returned free memory to 11 GB.

It is not a leak: `current_allocated_memory` never moved off 594 MB and
`gc.collect()` changed nothing. Only `empty_cache()` returns it.

**Why a high-water mark rather than releasing after every call.** The same
endpoints serve two very different workloads. Ingest sends batches of 64
variable-length chunks, which is what ratchets. Search sends a *single* query
string on every request, and `/rerank` sits on that same request path. Draining
unconditionally would throw away the cache the next query is about to reuse, on
an interactive path whose latency budget is already tight — and the reranker has
no concurrency gate, so one thread's drain would pull the cache out from under
the others mid-flight. Gating on the gap means small requests never pay, and the
ratchet is still bounded.

Call this on the worker thread, never the event loop: `empty_cache` blocks, and
starving `/health` is exactly what #553 was about.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Total parse — this runs at import, so a bad value must not stop startup."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        if raw:
            logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < 0:
        # Clamping to 0 would land on the "off" sentinel, so an operator typing
        # -1 to mean "no limit" would silently get the unbounded growth back
        # with no log line. Fail loudly toward the safe value instead.
        logger.warning("%s=%d is negative; using %d", name, value, default)
        return default
    return value


# Release once the cached-but-unused pool exceeds this. Measured on the Mac
# mini, the two workloads separate by more than an order of magnitude:
#
#     search steady state (single-query encodes)   0.44 GB   flat
#     after three ingest batches of 64             8.22 GB
#
# 4 GB sits well clear of both: search never triggers a drain, and the ratchet
# is caught long before it costs swap. A first attempt at 2 GB was too low —
# searches issued right after an ingest still sat above the mark and drained on
# every request, which is the interactive path this gate exists to protect.
CACHE_HIGH_WATER_MB = _int_env("GPU_CACHE_HIGH_WATER_MB", 4096)

# 0 disables entirely, restoring PyTorch's default behaviour.
ENABLED = CACHE_HIGH_WATER_MB > 0

_last_warned_at = float("-inf")
_WARN_INTERVAL_SECONDS = 300.0


def _warn(msg: str) -> None:
    """Rate-limited rather than once-per-process: this guards an OOM-class
    outage, and a single log line hours ago is not a signal anyone will see."""
    global _last_warned_at
    now = time.monotonic()
    if now - _last_warned_at >= _WARN_INTERVAL_SECONDS:
        _last_warned_at = now
        logger.warning(msg, exc_info=True)


def release_gpu_cache() -> None:
    """Drain the device allocator's free pool if it has grown past the mark.

    Silent no-op on CPU-only deployments (Docker), where there is no device
    allocator. Never raises — cache hygiene must not fail an inference call.
    """
    if not ENABLED:
        return
    try:
        import torch

        threshold = CACHE_HIGH_WATER_MB * 1024 * 1024
        if torch.backends.mps.is_available():
            gap = torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()
            if gap > threshold:
                torch.mps.empty_cache()
        elif torch.cuda.is_available():
            gap = torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
            if gap > threshold:
                torch.cuda.empty_cache()
    except Exception:  # never fail an encode over cache hygiene
        _warn("could not release GPU cache; continuing")
