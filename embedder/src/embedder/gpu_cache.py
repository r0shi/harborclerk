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
unconditionally throws away the cache the next query is about to reuse, on an
interactive path whose latency budget is already tight. Gating on the gap means
small requests never pay, and the ratchet is still bounded.

**Why a cooldown as well.** `empty_cache()` can only release heaps with no live
buffers, so a drain issued while another request holds activations reclaims
almost nothing — measured at 8 MB of 1024. Without a cooldown the gap stays
above the mark, so the *next* request drains too, and every one after it: the
gate degrades into firing constantly and reclaiming nothing, under exactly the
concurrent load that triggers it. (An earlier draft of this comment claimed the
hazard was one thread pulling the cache out from under another. That is not
possible — live tensors are never freed by `empty_cache` — and the real
pathology is the opposite one.)

Call this on the worker thread, never the event loop: `empty_cache` blocks, and
starving `/health` is exactly what #553 was about.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


# Larger than the unified memory of any Mac this ships on, so a value above it
# is a typo or a misunderstanding rather than a configuration.
_MAX_SANE_HIGH_WATER_MB = 512 * 1024


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
    if value > _MAX_SANE_HIGH_WATER_MB:
        # A value larger than any plausible machine is "off" in effect, which is
        # the same failure the negative branch exists to prevent — just spelled
        # differently. 0 stays a deliberate, documented disable.
        logger.warning("%s=%d exceeds any plausible device memory; using %d", name, value, default)
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

# Minimum gap after an *ineffective* drain. A drain costs 3-17ms for ~1 GB, so
# the point is not the cost of one — it is that under concurrency a drain can
# reclaim nothing while activations pin the heaps, leaving the gap above the
# mark and every subsequent request trying again.
#
# Gated on ineffectiveness rather than on time alone, because a blanket
# interval defeats the `finally` at both call sites. embedder_client retries a
# 5xx up to 7 times with cumulative minimum elapsed of 0.25/0.75/1.75/3.75s
# before attempts 2-5 — all inside a flat 5s window — so an OOM would drain
# once and then skip every retry, which is precisely the case the `finally`
# was added for. And the first drain is the one least likely to work: it runs
# while the failed encode's intermediates are still reachable through the
# in-flight traceback.
_MIN_DRAIN_INTERVAL_SECONDS = 5.0
# A drain that recovers less than this did not work — the heaps were pinned by
# something live. Only *those* start a cooldown.
_MIN_USEFUL_RECLAIM_BYTES = 64 * 1024 * 1024
_last_drain_at = float("-inf")


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
    global _last_drain_at
    if not ENABLED:
        return
    try:
        import torch

        if torch.backends.mps.is_available():
            measure, drain, label = _mps_gap, torch.mps.empty_cache, "mps"
        elif torch.cuda.is_available():
            measure, drain, label = _cuda_gap, torch.cuda.empty_cache, "cuda"
        else:
            return  # CPU-only: no device allocator to drain

        gap = measure(torch)
        if gap <= CACHE_HIGH_WATER_MB * 1024 * 1024:
            return

        now = time.monotonic()
        if now - _last_drain_at < _MIN_DRAIN_INTERVAL_SECONDS:
            return

        drain()
        after = measure(torch)
        reclaimed = gap - after

        # Cool down only if that achieved nothing. A drain that worked is
        # evidence the heaps are releasable, so blocking the next one would
        # defeat the `finally` at both call sites: embedder_client retries a 5xx
        # up to 7 times, with cumulative minimum elapsed of 0.25/0.75/1.75/3.75s
        # before attempts 2-5 — all inside a flat 5s window.
        _last_drain_at = now if reclaimed < _MIN_USEFUL_RECLAIM_BYTES else float("-inf")

        # INFO, not DEBUG: both services hard-code basicConfig(level=INFO) with
        # no override, so a debug line is unreachable in every shipped
        # deployment — and this is the only signal distinguishing "working" from
        # "firing constantly and recovering nothing".
        logger.info(
            "%s cache drained: %.0f MB -> %.0f MB (reclaimed %.0f MB)",
            label,
            gap / 1024 / 1024,
            after / 1024 / 1024,
            reclaimed / 1024 / 1024,
        )
    except Exception:  # never fail an encode over cache hygiene
        _warn("could not release GPU cache; continuing")


def _mps_gap(torch) -> int:
    return torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()


def _cuda_gap(torch) -> int:
    return torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
