"""Reranker integration — POST passages to /rerank, return reordered hits."""

from __future__ import annotations

import dataclasses
import logging
from typing import Literal

import httpx

from harbor_clerk.config import get_settings as _settings
from harbor_clerk.search_types import SearchHit

logger = logging.getLogger(__name__)


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

    On HTTP failure:
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

    try:
        async with httpx.AsyncClient(timeout=settings.reranker_timeout_seconds) as client:
            r = await client.post(
                f"{settings.reranker_url}/rerank",
                json={"query": query, "passages": passages, "top_k": top_k},
            )
            r.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        if settings.reranker_strict:
            raise
        logger.warning("reranker call failed; falling back to hybrid-only top-K: %r", exc)
        fallback = hits[:top_k]
        return (fallback, "failed") if return_status else fallback

    body = r.json()
    reordered: list[SearchHit] = []
    for entry in body["scores"]:
        idx = entry["index"]
        score = entry["score"]
        h = pool[idx]
        reordered.append(dataclasses.replace(h, score=score))

    result = reordered[:top_k]
    return (result, "ok") if return_status else result
