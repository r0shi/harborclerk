"""Tests for gpu_cache — the module's actual work, not just its call sites.

An earlier version of these tests covered only the wiring: every one either
patched `release_gpu_cache` out, forced `import torch` to fail, or
short-circuited on the disable flag. Replacing the whole function body with
`pass` kept them all green. These drive the real branches with a fake torch.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


def _fake_torch(*, mps=False, cuda=False, driver=0, current=0, reserved=0, allocated=0):
    """A stand-in exposing exactly the surface gpu_cache touches."""
    calls = []
    t = types.ModuleType("torch")

    t.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    t.mps = types.SimpleNamespace(
        is_available=lambda: mps,
        driver_allocated_memory=lambda: driver,
        current_allocated_memory=lambda: current,
        empty_cache=lambda: calls.append("mps"),
    )
    t.cuda = types.SimpleNamespace(
        is_available=lambda: cuda,
        memory_reserved=lambda: reserved,
        memory_allocated=lambda: allocated,
        empty_cache=lambda: calls.append("cuda"),
    )
    return t, calls


@pytest.fixture
def gpu_cache(monkeypatch):
    """Fresh module import so env-derived constants are re-read per test."""

    def _load(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("embedder.gpu_cache", None)
        return importlib.import_module("embedder.gpu_cache")

    yield _load
    # monkeypatch.undo() runs *after* this finalizer (it is a dependency, so it
    # is torn down last), so re-importing here would re-read the env var the
    # test just set and leak that value into sys.modules for whatever runs
    # next. Clear it first.
    monkeypatch.delenv("GPU_CACHE_HIGH_WATER_MB", raising=False)
    sys.modules.pop("embedder.gpu_cache", None)
    importlib.import_module("embedder.gpu_cache")


MB = 1024 * 1024


def test_releases_mps_once_over_the_high_water_mark(gpu_cache, monkeypatch):
    """The ratchet this module exists to stop."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="2048")
    torch, calls = _fake_torch(mps=True, driver=5000 * MB, current=500 * MB)  # 4.4 GB gap
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()

    assert calls == ["mps"], "a gap well past the mark must drain"


def test_leaves_a_small_cache_alone(gpu_cache, monkeypatch):
    """Search sends a single query per request; draining then would throw away
    the cache the next query reuses, on an already-tight latency budget."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="2048")
    torch, calls = _fake_torch(mps=True, driver=900 * MB, current=500 * MB)  # 400 MB gap
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()

    assert calls == [], "a small gap must not trigger a drain"


def test_prefers_mps_over_cuda(gpu_cache, monkeypatch):
    """Branch order matters: on a Mac both could report available."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1")
    torch, calls = _fake_torch(mps=True, cuda=True, driver=5000 * MB, current=0, reserved=5000 * MB, allocated=0)
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()

    assert calls == ["mps"]


def test_releases_cuda_when_that_is_the_backend(gpu_cache, monkeypatch):
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    torch, calls = _fake_torch(mps=False, cuda=True, reserved=4000 * MB, allocated=100 * MB)
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()

    assert calls == ["cuda"]


def test_noop_on_cpu_only(gpu_cache, monkeypatch):
    """Docker deployments have no device allocator to drain."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    torch, calls = _fake_torch(mps=False, cuda=False)
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()

    assert calls == []


def test_zero_disables_entirely(gpu_cache, monkeypatch):
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="0")
    torch, calls = _fake_torch(mps=True, driver=99_000 * MB, current=0)
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert mod.ENABLED is False
    mod.release_gpu_cache()
    assert calls == []


def test_never_raises_when_the_backend_misbehaves(gpu_cache, monkeypatch):
    """Cache hygiene must not fail an inference call."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    torch = types.ModuleType("torch")

    def _boom():
        raise RuntimeError("MPS backend exploded")

    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=_boom))
    monkeypatch.setitem(sys.modules, "torch", torch)

    mod.release_gpu_cache()  # must not raise


def test_high_water_env_is_total(gpu_cache):
    """Parsed at import, so junk must not stop the service binding."""
    for raw in ("", "auto", "  "):
        mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB=raw)
        assert mod.ENABLED, f"{raw!r} silently disabled the guard"
    assert gpu_cache(GPU_CACHE_HIGH_WATER_MB="512").CACHE_HIGH_WATER_MB == 512


def test_a_negative_value_does_not_silently_disable_the_guard(gpu_cache):
    """`max(0, int(raw))` parsed -1 fine and clamped it onto the "off"
    sentinel — so an operator typing -1 to mean "no limit" got the unbounded
    growth back with no log line. The old test asserted `>= 0`, enshrining it."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="-1")
    assert mod.ENABLED, "-1 disabled the guard"
    assert mod.CACHE_HIGH_WATER_MB == 4096


def test_the_first_failure_is_logged(gpu_cache, monkeypatch, caplog):
    """`_last_warned_at = 0.0` compared against boot-relative monotonic()
    suppressed every warning for the first 300s of uptime — which is exactly
    when the menubar app autostarts the embedder at login."""
    import logging
    import types

    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    # Replace the module reference, not stdlib `time.monotonic` — the latter
    # freezes every thread in the process, including event-loop clocks.
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: 100.0))

    torch = types.ModuleType("torch")

    def _boom():
        raise RuntimeError("backend exploded")

    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=_boom))
    monkeypatch.setitem(sys.modules, "torch", torch)

    with caplog.at_level(logging.WARNING):
        mod.release_gpu_cache()

    assert "could not release GPU cache" in caplog.text, "first failure was swallowed"


def test_a_drain_that_reclaims_nothing_does_not_repeat_immediately(gpu_cache, monkeypatch):
    """`empty_cache()` cannot release heaps holding live buffers.

    Measured: with another request's activations in flight, a drain recovered
    8 MB of 1024. Without a cooldown the gap stays above the mark, so the next
    request drains too — and every one after it. The gate degrades into firing
    constantly and reclaiming nothing, under exactly the concurrent load that
    triggers it.
    """
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    # A gap that a drain cannot reduce, because something else holds the heaps.
    torch, calls = _fake_torch(mps=True, driver=5000 * MB, current=100 * MB)
    monkeypatch.setitem(sys.modules, "torch", torch)

    clock = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    for _ in range(3):
        mod.release_gpu_cache()

    assert len(calls) == 1, f"drained {len(calls)} times in 0.2s; the cooldown is not holding"


def test_the_cooldown_expires(gpu_cache, monkeypatch):
    """It is a cooldown, not a one-shot — a genuine later ratchet must drain."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")
    torch, calls = _fake_torch(mps=True, driver=5000 * MB, current=100 * MB)
    monkeypatch.setitem(sys.modules, "torch", torch)

    # One monotonic() read per release_gpu_cache() that reaches the gate.
    times = iter([0.0, 100.0])
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: next(times)))

    mod.release_gpu_cache()
    mod.release_gpu_cache()

    assert len(calls) == 2, "cooldown never expired"


def test_an_absurd_high_water_is_rejected(gpu_cache):
    """A value past any real device is as 'off' as 0, just spelled differently."""
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="999999999")
    assert mod.CACHE_HIGH_WATER_MB == 4096
    assert mod.ENABLED


def test_an_effective_drain_does_not_start_a_cooldown(gpu_cache, monkeypatch):
    """A blanket interval defeats the `finally` at both call sites.

    embedder_client retries a 5xx up to 7 times, with cumulative minimum elapsed
    of 0.25/0.75/1.75/3.75s before attempts 2-5 — all inside a flat 5s window.
    An OOM would drain once and then skip every retry, which is exactly the case
    the `finally` exists for. So a drain that *worked* must not block the next.
    """
    mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB="1024")

    # A backend whose drain genuinely frees the pool.
    state = {"driver": 5000 * MB}
    calls = []
    torch = types.ModuleType("torch")

    def _drain():
        calls.append("mps")
        state["driver"] = 200 * MB

    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True))
    torch.mps = types.SimpleNamespace(
        driver_allocated_memory=lambda: state["driver"],
        current_allocated_memory=lambda: 0,
        empty_cache=_drain,
    )
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: 0.0))

    mod.release_gpu_cache()
    assert calls == ["mps"]

    # Pool ratchets straight back up, still within the interval.
    state["driver"] = 5000 * MB
    mod.release_gpu_cache()

    assert len(calls) == 2, "an effective drain started a cooldown and blocked the retry-path drain"
