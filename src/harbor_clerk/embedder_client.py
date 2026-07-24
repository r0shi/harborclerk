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
Everything that decides *anything* — `_retry_after`, `_parse`, `_backoff_delay`
— is shared, so the two loops cannot drift on policy; they differ only in how
they sleep and how they issue the request.

Retrying is for callers with no fallback. `search._embed_query` passes
`max_attempts=1` on purpose: it already degrades to lexical-only for free, so
waiting out a backoff would buy an identical result more slowly.
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
#     4 attempts ->  1.75 -  3.50s  (shorter than a restart; would still fail)
#     6 attempts ->  7.75 - 15.50s  (covers 7.2s, but by only 0.55s)
#     7 attempts -> 11.75 - 23.50s  (covers it with 1.5x margin)
#
# 4 was the original guess and it was wrong — a live ingest showed a document
# failing across a restart the retry was supposed to absorb. 6 cleared the one
# measurement by 7%, which is not margin, it is luck; a slower disk or a bigger
# model would quietly put it back under. The test asserts the margin, not the
# measurement, so this cannot regress silently.
DEFAULT_MAX_ATTEMPTS = 7

# Hard ceiling on one call, retries included. Without it, a hung embedder that
# accepts but never answers costs `max_attempts * timeout` per batch — with the
# embed stage's 120s timeout that is ~735s, so five batches would trip the
# stage's own 3600s alarm and surface as "Stage embed timed out" instead of the
# EmbedderError that says what actually happened.
DEFAULT_DEADLINE_SECONDS = 300.0
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


def _parse(resp: httpx.Response) -> list[list[float]]:
    """Turn a non-retryable response into vectors, or raise EmbedderError.

    A 200 carrying a proxy error page or an unexpected shape used to escape as
    a raw `json.JSONDecodeError` / `KeyError`, past callers that only expect
    EmbedderError.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise EmbedderError(f"embedder rejected request: HTTP {resp.status_code}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise EmbedderError(f"embedder returned a non-JSON body (HTTP {resp.status_code})") from exc
    try:
        return data["embeddings"]
    except (KeyError, TypeError) as exc:
        raise EmbedderError("embedder response has no 'embeddings' field") from exc


def _payload(texts: list[str], task: str) -> dict:
    return {"texts": texts, "task": task}


def _give_up(attempts_made: int, max_attempts: int, detail: str) -> EmbedderError:
    return EmbedderError(f"embedder call failed after {attempts_made}/{max_attempts} attempts: {detail}")


def embed_texts(
    texts: list[str],
    *,
    task: str,
    timeout: float = 120.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> list[list[float]]:
    """Embed `texts` synchronously, retrying transient failures.

    Raises EmbedderError on unretryable failure or once retries are exhausted.
    """
    settings = get_settings()
    url = f"{settings.embedder_url}/embed"
    last_detail = "no attempt made"
    started = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.post(url, json=_payload(texts, task), timeout=timeout)
        except httpx.TransportError as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
        except httpx.RequestError as exc:
            # DecodingError / TooManyRedirects are RequestError but NOT
            # TransportError. Retrying will not help, but the caller is still
            # owed an EmbedderError rather than a raw httpx type.
            raise EmbedderError(f"embedder request failed: {type(exc).__name__}: {exc}") from exc
        else:
            if not _is_retryable_status(resp.status_code):
                # Every 2xx and every non-429 4xx: succeed or fail, never retry.
                return _parse(resp)
            last_detail = f"HTTP {resp.status_code}"

        if attempt == max_attempts or time.monotonic() - started >= deadline_seconds:
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
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> list[list[float]]:
    """Embed `texts` on the event loop, retrying transient failures.

    Raises EmbedderError on unretryable failure or once retries are exhausted.
    """
    settings = get_settings()
    url = f"{settings.embedder_url}/embed"
    last_detail = "no attempt made"
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(url, json=_payload(texts, task))
            except httpx.TransportError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            except httpx.RequestError as exc:
                raise EmbedderError(f"embedder request failed: {type(exc).__name__}: {exc}") from exc
            else:
                if not _is_retryable_status(resp.status_code):
                    return _parse(resp)
                last_detail = f"HTTP {resp.status_code}"

            if attempt == max_attempts or time.monotonic() - started >= deadline_seconds:
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
