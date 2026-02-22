from redis.asyncio import from_url as redis_from_url

from harbor_clerk.config import get_settings


def get_async_redis():
    """Create an async Redis client."""
    settings = get_settings()
    return redis_from_url(settings.redis_url, decode_responses=True)
