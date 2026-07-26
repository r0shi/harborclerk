"""Return GPU allocator cache to the OS after inference.

PyTorch's caching allocator keeps freed device blocks for reuse and never
returns them. That is normally self-limiting — a steady workload reaches a
steady state and the cache is pure win. It is not self-limiting here, because
the inputs are documents and email bodies whose lengths vary by orders of
magnitude: nearly every batch asks for a shape the cache has not seen, so it
allocates a fresh block and the cache ratchets upward forever.

Measured on the Mac mini (M4, 32 GB), embedding a real corpus:

    fresh embedder process          1.5 GB
    after ~1.5 hours of ingest       40 GB   (phys_footprint, peak 44 GB)

At that point the machine had 347 MB free, 15 GB in the compressor and 13 GB of
swap in use. Restarting the embedder returned free memory to 11 GB.

Ten batches of 64 variable-length texts, with and without releasing:

    unmanaged (GB)  6.7 12.8  6.5  8.6  8.6  9.6 11.9 16.3 11.5 12.4
    managed   (GB)  4.1  8.6  2.4  2.9  2.9  2.3  3.0  7.4  2.6  3.5

Managed still spikes while a large batch is in flight — that is live
allocation, not cache — but it returns to baseline instead of ratcheting.

Releasing costs nothing measurable (97.2s vs 96.8s over five batches) and on a
machine this tight it is slightly *faster*, because holding gigabytes of dead
cache costs more in compression and swap than re-allocating does.

Call this on the worker thread, never the event loop: `empty_cache` blocks, and
starving `/health` is exactly what #553 was about.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Escape hatch. Set to 0/false to keep PyTorch's default caching behaviour, e.g.
# on a machine with headroom to spare where the reuse is worth more than the RAM.
RELEASE_GPU_CACHE = os.environ.get("RELEASE_GPU_CACHE", "true").lower() not in ("0", "false", "no")

_warned = False


def release_gpu_cache() -> None:
    """Hand cached device memory back, on whichever backend is active.

    Silent no-op on CPU-only deployments (Docker), where there is no device
    allocator to drain.
    """
    global _warned
    if not RELEASE_GPU_CACHE:
        return
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — never fail an encode over cache hygiene
        if not _warned:
            logger.warning("could not release GPU cache; continuing", exc_info=True)
            _warned = True
