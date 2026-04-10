"""In-memory sliding window rate limiter with crash recovery."""

import asyncio
import logging
import time
import uuid
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """Per-key sliding window rate limiter.

    Maintains a deque of request timestamps per key. check() counts
    entries in the last 60s/3600s and compares against limits.

    Thread-safe via per-key asyncio.Lock (prevents double-seeding
    and ensures check-and-record is atomic).
    """

    def __init__(self):
        self._windows: dict[uuid.UUID, deque[float]] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._seeded: set[uuid.UUID] = set()

    def _get_lock(self, key_id: uuid.UUID) -> asyncio.Lock:
        if key_id not in self._locks:
            self._locks[key_id] = asyncio.Lock()
        return self._locks[key_id]

    def _get_window(self, key_id: uuid.UUID) -> deque[float]:
        if key_id not in self._windows:
            self._windows[key_id] = deque()
        return self._windows[key_id]

    async def _seed_if_needed(self, key_id: uuid.UUID) -> None:
        """Seed the window from api_request_log on first access after startup."""
        if key_id in self._seeded:
            return
        try:
            from datetime import UTC, datetime, timedelta

            from sqlalchemy import select

            from harbor_clerk.db import async_session_factory
            from harbor_clerk.models.api_request_log import ApiRequestLog

            async with async_session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(ApiRequestLog.created_at)
                            .where(
                                ApiRequestLog.api_key_id == key_id,
                                ApiRequestLog.created_at >= datetime.now(UTC) - timedelta(hours=1),
                                ApiRequestLog.status.in_(["ok", "error"]),
                            )
                            .order_by(ApiRequestLog.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )

                window = self._get_window(key_id)
                for ts in rows:
                    window.append(ts.timestamp())
        except Exception:
            logger.debug("Failed to seed rate limiter for key %s", key_id, exc_info=True)
        self._seeded.add(key_id)

    def _prune(self, window: deque[float], now: float) -> None:
        """Remove entries older than 1 hour."""
        cutoff = now - 3600
        while window and window[0] < cutoff:
            window.popleft()

    async def check(
        self,
        key_id: uuid.UUID,
        rpm_limit: int,
        rph_limit: int,
    ) -> tuple[bool, int]:
        """Check rate limit and record the request if allowed.

        Returns (allowed, retry_after_seconds).
        If allowed is True, the request timestamp is recorded.
        If allowed is False, retry_after_seconds indicates when to retry.
        rpm_limit/rph_limit of 0 means unlimited for that window.
        """
        if rpm_limit == 0 and rph_limit == 0:
            return True, 0

        lock = self._get_lock(key_id)
        async with lock:
            await self._seed_if_needed(key_id)
            window = self._get_window(key_id)
            now = time.time()
            self._prune(window, now)

            # Check per-minute limit
            if rpm_limit > 0:
                minute_cutoff = now - 60
                minute_count = sum(1 for t in window if t >= minute_cutoff)
                if minute_count >= rpm_limit:
                    oldest_in_minute = next(t for t in window if t >= minute_cutoff)
                    retry_after = int(oldest_in_minute + 60 - now) + 1
                    return False, max(retry_after, 1)

            # Check per-hour limit
            if rph_limit > 0:
                hour_count = len(window)
                if hour_count >= rph_limit:
                    retry_after = int(window[0] + 3600 - now) + 1
                    return False, max(retry_after, 1)

            # Allowed — record timestamp
            window.append(now)
            return True, 0


# Module-level singleton
rate_limiter = RateLimiter()
