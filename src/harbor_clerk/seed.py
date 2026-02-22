"""Seed module — initial admin is now created via /api/setup."""

import logging

logger = logging.getLogger(__name__)


async def seed_admin_user() -> None:
    """No-op. Initial admin is created via the first-time setup flow."""
    logger.debug("seed_admin_user is a no-op; use POST /api/setup instead")
