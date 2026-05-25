"""Resolve CLI configuration from env vars and CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CliConfig:
    url: str
    api_key: str
    insecure: bool


def resolve_config(
    *,
    url: str | None,
    api_key: str | None,
    insecure: bool,
) -> CliConfig:
    """Resolve config from flags first, then env vars, then defaults."""
    resolved_url = url or os.environ.get("HARBOR_CLERK_URL") or "https://localhost"
    resolved_api_key = api_key or os.environ.get("HARBOR_CLERK_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "Missing API key. Set HARBOR_CLERK_API_KEY or pass --api-key. Generate a key in System Settings → API Keys."
        )
    resolved_insecure = insecure or _truthy(os.environ.get("HARBOR_CLERK_INSECURE_SKIP_VERIFY"))
    return CliConfig(
        url=resolved_url.rstrip("/"),
        api_key=resolved_api_key,
        insecure=resolved_insecure,
    )
