"""Embedder HTTP client with bounded retry for transient failures.

The embedder is a local, single-worker service. A service restart, a model
reload, or several `cpu` workers hitting it at once all surface as connection
errors, read timeouts, or 5xx — none of which mean the request was wrong. Before
this module existed, one such blip failed the whole `embed` stage and the
document landed `error`, needing a manual reprocess (#552).

A 4xx is the opposite: the request itself is malformed and will never succeed.
Retrying it burns the budget and delays the real error, so we fail fast.

429 is grouped with the retryable set — it means "later", not "never".

Both a sync and an async entry point exist because the two callers differ:
the `embed` stage runs in a sync worker, query embedding runs on the event loop.
The retry policy lives in `_classify` so the two cannot drift apart.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)

# The retry window has to outlast a full embedder restart, because that is the
# failure it exists for. Measured on the Mac mini: shutdown 15:30:17.9 ->
# model loaded 15:30:25.1, so the service is unreachable for ~7.2s.
#
# N attempts sleep after the first N-1 of them, and the jitter halves each
# ceiling, so the real window is 0.5*sum .. sum of the ceilings:
#
#     4 attempts -> 1.75 - 3.50s   (shorter than a restart; would still fail)
#     6 attempts -> 7.75 - 15.50s  (covers it, with margin)
#
# 4 was the original guess and it was wrong — a live ingest showed a document
# failing across a restart the retry was supposed to absorb.
DEFAULT_MAX_ATTEMPTS = 6
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0


class EmbedderError(RuntimeError):
    """Embedder call failed after exhausting retries, or failed unretryably."""


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with half-to-full jitter.

    Jitter matters more than usual here: the `cpu` workers are woken by the same
    LISTEN notification, so un-jittered retries would resynchronise them into
    the same thundering herd that caused the failure.
    """
    ceiling = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return ceiling * (0.5 + random.random() * 0.5)


def _is_retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _payload(texts: list[str], task: str) -> dict:
    return {"texts": texts, "task": task}


def _extract(data: dict) -> list[list[float]]:
    return data["embeddings"]


def _give_up(attempt: int, max_attempts: int, detail: str) -> EmbedderError:
    return EmbedderError(f"embedder call failed after {attempt}/{max_attempts} attempts: {detail}")


def embed_texts(
    texts: list[str],
    *,
    task: str,
    timeout: float = 120.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[list[float]]:
    """Embed `texts` synchronously, retrying transient failures.

    Raises EmbedderError on unretryable failure or once retries are exhausted.
    """
    settings = get_settings()
    url = f"{settings.embedder_url}/embed"
    last_detail = "no attempt made"

    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.post(url, json=_payload(texts, task), timeout=timeout)
        except httpx.TransportError as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
        else:
            if not _is_retryable_status(resp.status_code):
                # Includes every 2xx and every non-429 4xx. raise_for_status
                # turns the latter into an exception we deliberately do not retry.
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise EmbedderError(f"embedder rejected request: HTTP {resp.status_code}") from exc
                return _extract(resp.json())
            last_detail = f"HTTP {resp.status_code}"

        if attempt == max_attempts:
            break
        delay = _backoff_delay(attempt)
        logger.warning(
            "embedder call failed (%s), attempt %d/%d, retrying in %.1fs",
            last_detail,
            attempt,
            max_attempts,
            delay,
        )
        time.sleep(delay)

    raise _give_up(max_attempts, max_attempts, last_detail)


async def embed_texts_async(
    texts: list[str],
    *,
    task: str,
    timeout: float = 30.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[list[float]]:
    """Embed `texts` on the event loop, retrying transient failures.

    Raises EmbedderError on unretryable failure or once retries are exhausted.
    """
    settings = get_settings()
    url = f"{settings.embedder_url}/embed"
    last_detail = "no attempt made"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(url, json=_payload(texts, task))
            except httpx.TransportError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            else:
                if not _is_retryable_status(resp.status_code):
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise EmbedderError(f"embedder rejected request: HTTP {resp.status_code}") from exc
                    return _extract(resp.json())
                last_detail = f"HTTP {resp.status_code}"

            if attempt == max_attempts:
                break
            delay = _backoff_delay(attempt)
            logger.warning(
                "embedder query call failed (%s), attempt %d/%d, retrying in %.1fs",
                last_detail,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)

    raise _give_up(max_attempts, max_attempts, last_detail)
