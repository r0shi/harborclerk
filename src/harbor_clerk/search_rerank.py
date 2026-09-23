"""Reranker integration — POST passages to /rerank, return reordered hits."""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import Literal

import httpx

from harbor_clerk.config import get_settings as _settings
from harbor_clerk.search_types import SearchHit

logger = logging.getLogger(__name__)


class RerankResponseError(RuntimeError):
    """The reranker answered 200 with a body we cannot apply.

    Deliberately not a ValueError: api/routes/search.py maps a ValueError out of
    hybrid_search to a 422 (meant for "doc_id and doc_ids are exclusive"), and a
    reranker malfunction under ``reranker_strict`` is a server fault, not a
    client error.
    """


def _reorder_by_response(pool: list[SearchHit], body: dict) -> list[SearchHit]:
    """Apply a ``/rerank`` body to ``pool``; raise RerankResponseError on any bad entry.

    Validated before anything is applied, so a reranking is never installed
    partially. A NaN from the CrossEncoder arrives here as JSON ``null`` (#699);
    an index outside the pool would raise IndexError mid-loop.
    """
    entries = body.get("scores") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise RerankResponseError("response has no 'scores' list")
    reordered: list[SearchHit] = []
    seen: set[int] = set()
    for pos, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RerankResponseError(f"entry {pos} is not an object")
        idx = entry.get("index")
        score = entry.get("score")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(pool):
            raise RerankResponseError(f"entry {pos} has index {idx!r} outside the pool of {len(pool)}")
        if idx in seen:
            # Would return one chunk twice and silently drop another.
            raise RerankResponseError(f"entry {pos} repeats index {idx}")
        seen.add(idx)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise RerankResponseError(f"entry {pos} (index {idx}) has a non-finite score {score!r}")
        reordered.append(dataclasses.replace(pool[idx], score=float(score)))
    return reordered


def _format_passage(hit: SearchHit) -> str:
    """Reranker input: include doc title for cross-encoder context."""
    return f"Title: {hit.doc_title or ''}\n\nChunk: {hit.chunk_text}"


async def rerank_hits(
    query: str,
    hits: list[SearchHit],
    top_k: int,
    *,
    return_status: bool = False,
) -> list[SearchHit] | tuple[list[SearchHit], Literal["ok", "disabled", "failed"]]:
    """Call the reranker service; reorder ``hits`` by reranker score; return top_k.

    On HTTP failure, or a 200 whose body cannot be applied (a ``null`` or
    non-finite score, an index outside the pool):
      - if ``settings.reranker_strict`` is True, propagate the exception
      - otherwise log a warning and return ``hits[:top_k]`` in the original order

    ``hits`` are silently truncated to ``settings.reranker_pool_size`` before
    being sent (caps reranker latency at predictable bounds).

    When ``return_status=True``, returns ``(hits, status)`` where status is
    one of "ok" / "failed" so the caller can populate ``SearchResponse.reranker_status``.
    """
    settings = _settings()
    if not hits:
        return ([], "disabled") if return_status else []

    pool = hits[: settings.reranker_pool_size]
    passages = [_format_passage(h) for h in pool]
    effective_top_k = min(top_k, len(pool))

    try:
        async with httpx.AsyncClient(timeout=settings.reranker_timeout_seconds) as client:
            r = await client.post(
                f"{settings.reranker_url}/rerank",
                json={"query": query, "passages": passages, "top_k": effective_top_k},
            )
            r.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        if settings.reranker_strict:
            raise
        logger.warning("reranker call failed; falling back to hybrid-only top-K: %r", exc)
        fallback = hits[:top_k]
        return (fallback, "failed") if return_status else fallback

    try:
        try:
            body = r.json()
        except ValueError as exc:  # a 200 that is not JSON
            raise RerankResponseError(f"response body is not JSON: {exc}") from exc
        reordered = _reorder_by_response(pool, body)
    except RerankResponseError as exc:
        if settings.reranker_strict:
            raise
        logger.warning("reranker response unusable; falling back to hybrid-only top-K: %s", exc)
        fallback = hits[:top_k]
        return (fallback, "failed") if return_status else fallback

    result = reordered[:top_k]
    return (result, "ok") if return_status else result
