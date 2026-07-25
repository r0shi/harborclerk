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
Everything that decides anything — `_is_retryable_status`,
`_is_retryable_transport`, `_parse`, `_backoff_delay` — is shared, so the two
loops cannot drift on policy; they differ only in how they sleep and how they
issue the request. (429 is retried with plain backoff; the `Retry-After` header
is not honoured.)

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

# Stop starting new attempts once this much time has gone. It bounds the retry
# loop, not a single request: the check runs between attempts, so the worst case
# is `deadline + timeout` — a final attempt may begin just under the line. With
# the embed stage's 120s per-request timeout that is ~420s, against the ~840s an unbounded
# 7-attempt budget would cost. The point is to keep a batch clear of the stage's
# own 3600s alarm, which would otherwise mask the real error behind
# "Stage embed timed out".
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


# Transport failures where the request never landed, or the connection died
# under it. These are the restart case — the embedder went away and came back —
# and retrying them is the entire point of this module.
_RETRYABLE_TRANSPORT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.CloseError,
)


def _is_retryable_transport(exc: httpx.TransportError) -> bool:
    """Whether re-sending this request can help — or would only add load.

    Deliberately excludes ReadTimeout and WriteTimeout. Those mean the request
    *was* delivered and the server is still working on it: the embedder holds
    one model behind a concurrency of 1, and a client-side timeout neither
    cancels the in-flight encode nor lets the server see the disconnect. So a
    retry does not replace the slow encode, it queues a second one behind it.
    With a 7-attempt budget a 64-text batch that merely runs long would cost up
    to seven duplicated encodes — turning a slow embedder into an overloaded
    one, which is the failure this module exists to absorb.

    UnsupportedProtocol is excluded too: a misconfigured EMBEDDER_URL is
    permanent, and retrying it once per batch of every document is pure waste.
    """
    return isinstance(exc, _RETRYABLE_TRANSPORT)


def _parse(resp: httpx.Response, expected: int) -> list[list[float]]:
    """Turn a non-retryable response into vectors, or raise EmbedderError.

    A 200 carrying a proxy error page or an unexpected shape used to escape as
    a raw `json.JSONDecodeError` / `KeyError`, past callers that only expect
    EmbedderError.

    The length check is not defensive padding. `run_embed` pairs the result with
    its batch using `zip`, which truncates silently — so a short list leaves the
    unpaired chunks at `embedding = NULL` while progress still counts the whole
    batch, `mark_stage_done` runs, and the document reaches `ready`. Those
    chunks are then invisible to vector search forever (`hybrid_search` filters
    on `embedding IS NOT NULL`) with nothing recorded to say a reprocess is
    needed. Failing the stage loudly is strictly better than losing them
    quietly.
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
        embeddings = data["embeddings"]
    except (KeyError, TypeError) as exc:
        raise EmbedderError("embedder response has no 'embeddings' field") from exc
    if not isinstance(embeddings, list):
        raise EmbedderError(f"embedder returned {type(embeddings).__name__} for 'embeddings', expected a list")
    if len(embeddings) != expected:
        raise EmbedderError(f"embedder returned {len(embeddings)} vectors for {expected} texts")
    return embeddings


def _payload(texts: list[str], task: str) -> dict:
    return {"texts": texts, "task": task}


def _give_up(attempts_made: int, max_attempts: int, detail: str) -> EmbedderError:
    """`attempts_made` is what actually ran, which is not always max_attempts.

    Exiting via the deadline stops early, and this string is the operator-facing
    diagnostic in `ingestion_jobs.error` — reporting "7/7 attempts" for 3 sends
    someone looking for a retry storm that never happened.
    """
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

    attempts_made = 0
    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt
        try:
            resp = httpx.post(url, json=_payload(texts, task), timeout=timeout)
        except httpx.TransportError as exc:
            if not _is_retryable_transport(exc):
                raise EmbedderError(f"embedder call failed: {type(exc).__name__}: {exc}") from exc
            last_detail = f"{type(exc).__name__}: {exc}"
        except httpx.RequestError as exc:
            # DecodingError / TooManyRedirects are RequestError but NOT
            # TransportError. Retrying will not help, but the caller is still
            # owed an EmbedderError rather than a raw httpx type.
            raise EmbedderError(f"embedder request failed: {type(exc).__name__}: {exc}") from exc
        else:
            if not _is_retryable_status(resp.status_code):
                # Every 2xx and every non-429 4xx: succeed or fail, never retry.
                return _parse(resp, len(texts))
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

    raise _give_up(attempts_made, max_attempts, last_detail)


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
        attempts_made = 0
        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            try:
                resp = await client.post(url, json=_payload(texts, task))
            except httpx.TransportError as exc:
                if not _is_retryable_transport(exc):
                    raise EmbedderError(f"embedder call failed: {type(exc).__name__}: {exc}") from exc
                last_detail = f"{type(exc).__name__}: {exc}"
            except httpx.RequestError as exc:
                raise EmbedderError(f"embedder request failed: {type(exc).__name__}: {exc}") from exc
            else:
                if not _is_retryable_status(resp.status_code):
                    return _parse(resp, len(texts))
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

    raise _give_up(attempts_made, max_attempts, last_detail)
