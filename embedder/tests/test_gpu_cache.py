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
    for raw in ("", "auto", "  ", "-5"):
        mod = gpu_cache(GPU_CACHE_HIGH_WATER_MB=raw)
        assert mod.CACHE_HIGH_WATER_MB >= 0
    assert gpu_cache(GPU_CACHE_HIGH_WATER_MB="512").CACHE_HIGH_WATER_MB == 512
