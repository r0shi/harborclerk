"""Tests for in-memory sliding window rate limiter."""

import asyncio
import time
import uuid

import pytest

from harbor_clerk.api.rate_limiter import RateLimiter


def _make_limiter_with_key() -> tuple[RateLimiter, uuid.UUID]:
    """Create a fresh limiter and pre-seeded key (skips DB seeding)."""
    limiter = RateLimiter()
    key = uuid.uuid4()
    limiter._seeded.add(key)
    return limiter, key


@pytest.mark.asyncio
async def test_allows_under_limit():
    limiter, key = _make_limiter_with_key()
    allowed, retry = await limiter.check(key, rpm_limit=10, rph_limit=100)
    assert allowed is True
    assert retry == 0


@pytest.mark.asyncio
async def test_blocks_over_rpm():
    limiter, key = _make_limiter_with_key()
    rpm = 5
    for _ in range(rpm):
        allowed, _ = await limiter.check(key, rpm_limit=rpm, rph_limit=0)
        assert allowed is True

    allowed, retry = await limiter.check(key, rpm_limit=rpm, rph_limit=0)
    assert allowed is False
    assert retry >= 1


@pytest.mark.asyncio
async def test_blocks_over_rph():
    limiter, key = _make_limiter_with_key()
    rph = 5
    for _ in range(rph):
        allowed, _ = await limiter.check(key, rpm_limit=0, rph_limit=rph)
        assert allowed is True

    allowed, retry = await limiter.check(key, rpm_limit=0, rph_limit=rph)
    assert allowed is False
    assert retry >= 1


@pytest.mark.asyncio
async def test_unlimited_always_allows():
    limiter, key = _make_limiter_with_key()
    for _ in range(100):
        allowed, retry = await limiter.check(key, rpm_limit=0, rph_limit=0)
        assert allowed is True
        assert retry == 0


@pytest.mark.asyncio
async def test_independent_keys():
    limiter = RateLimiter()
    key_a = uuid.uuid4()
    key_b = uuid.uuid4()
    limiter._seeded.add(key_a)
    limiter._seeded.add(key_b)

    # Fill key_a to its limit
    for _ in range(3):
        allowed, _ = await limiter.check(key_a, rpm_limit=3, rph_limit=0)
        assert allowed is True

    # key_a blocked
    allowed, _ = await limiter.check(key_a, rpm_limit=3, rph_limit=0)
    assert allowed is False

    # key_b still allowed
    allowed, _ = await limiter.check(key_b, rpm_limit=3, rph_limit=0)
    assert allowed is True


@pytest.mark.asyncio
async def test_prune_old_entries():
    limiter, key = _make_limiter_with_key()
    window = limiter._get_window(key)
    now = time.time()

    # Insert timestamps older than 1 hour
    window.append(now - 7200)
    window.append(now - 3700)
    # Insert one recent timestamp
    window.append(now - 30)

    limiter._prune(window, now)
    assert len(window) == 1
    assert window[0] == pytest.approx(now - 30, abs=0.01)


@pytest.mark.asyncio
async def test_concurrent_checks_atomic():
    limiter, key = _make_limiter_with_key()
    rpm = 5

    # Fill to limit - 1
    for _ in range(rpm - 1):
        allowed, _ = await limiter.check(key, rpm_limit=rpm, rph_limit=0)
        assert allowed is True

    # Fire two concurrent checks — exactly one should succeed
    results = await asyncio.gather(
        limiter.check(key, rpm_limit=rpm, rph_limit=0),
        limiter.check(key, rpm_limit=rpm, rph_limit=0),
    )
    allowed_count = sum(1 for allowed, _ in results if allowed)
    assert allowed_count == 1
