"""How much of the corpus semantic search can see: the share of active documents' chunks with a vector.

A chunk without an embedding is invisible to the vector leg; after #685 a
failing night left 29% of a corpus without one, and the only way to learn it
was to query Postgres. One aggregate serves both readers:

- `/system/stats` reports the counts (`embedding_coverage_counts`), uncached;
- search marks a result "partial" from the figure for its reach
  (`search_scope_coverage`): with no document filter, the corpus share
  (`corpus_embedding_coverage`), through a process-wide cache refreshed lazily
  at most once per CORPUS_COVERAGE_TTL_SECONDS. The aggregate is not free:
  measured at 125 ms warm and 4.1 s cold on 300k chunks, paid by the one search
  that finds the figure stale, once a minute per process.

Not over a query's candidates. That figure was diluted by the vector leg itself
(it only ever returns embedded chunks), so a corpus with a third of its
documents unembedded could read fully covered, and it scanned every chunk of up
to ~1,000 candidate documents per find_all.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Chunk, Document

logger = logging.getLogger(__name__)

# How long a corpus coverage figure stands before the next search refreshes it.
# Coverage moves as ingest embeds documents, minutes not seconds; one aggregate
# a minute keeps the cost off the search path.
CORPUS_COVERAGE_TTL_SECONDS = 60.0

# A document filter this small gets an exact figure over just its documents:
# one aggregate on chunks.doc_id (chunks_doc_idx), bounded by the filter. 50 is
# the doc_ids cap REST and MCP already enforce on a caller's own filter.
EXACT_COVERAGE_MAX_DOCS = 50

_cache: tuple[float, float | None] | None = None  # (monotonic time, share)
_refreshing = False


def _now() -> float:
    return time.monotonic()


async def embedding_coverage_counts(session: AsyncSession) -> dict[str, int]:
    """Chunk and document counts, with and without vectors, over active documents."""
    row = (
        await session.execute(
            select(
                func.count(Chunk.chunk_id),
                func.count(Chunk.embedding),
                func.count(func.distinct(Chunk.doc_id)),
                func.count(func.distinct(Chunk.doc_id)).filter(Chunk.embedding.is_(None)),
            )
            .join(Document, Document.doc_id == Chunk.doc_id)
            .where(Document.status == "active")
        )
    ).one()
    chunks_total, chunks_embedded, docs_with_chunks, docs_missing = (int(v or 0) for v in row)
    return {
        "chunks_total": chunks_total,
        "chunks_embedded": chunks_embedded,
        "chunks_missing": chunks_total - chunks_embedded,
        "documents_with_chunks": docs_with_chunks,
        "documents_fully_embedded": docs_with_chunks - docs_missing,
        "documents_missing_embeddings": docs_missing,
    }


async def corpus_embedding_coverage(session: AsyncSession) -> float | None:
    """The share of active chunks with a vector (None for an empty corpus), cached process-wide.

    A stale or missing figure is refreshed by the caller that finds it so, on
    its own session; a caller arriving while that refresh is in flight uses the
    previous figure rather than running a second aggregate.
    """
    global _cache, _refreshing
    cached = _cache
    if cached is not None and _now() - cached[0] < CORPUS_COVERAGE_TTL_SECONDS:
        return cached[1]
    if _refreshing:
        return cached[1] if cached is not None else None
    _refreshing = True
    try:
        counts = await embedding_coverage_counts(session)
        total = counts["chunks_total"]
        share = round(counts["chunks_embedded"] / total, 3) if total else None
        _cache = (_now(), share)
        return share
    finally:
        _refreshing = False


async def documents_embedding_coverage(session: AsyncSession, doc_ids: list) -> float | None:
    """The share of these active documents' chunks with a vector; None when they have none."""
    total, embedded = (
        await session.execute(
            select(func.count(Chunk.chunk_id), func.count(Chunk.embedding))
            .join(Document, Document.doc_id == Chunk.doc_id)
            .where(Chunk.doc_id.in_(list(doc_ids)), Document.status == "active")
        )
    ).one()
    return round(embedded / total, 3) if total else None


async def search_scope_coverage(session: AsyncSession, scope_doc_ids: list | None) -> float | None:
    """The vector coverage to report for a search reaching ``scope_doc_ids`` (None: no document filter).

    - No document filter: the cached corpus figure. Date, language, MIME and
      metadata filters do not narrow it; they are not document lists.
    - At most EXACT_COVERAGE_MAX_DOCS documents: the exact figure over them. A
      search limited to one fully embedded document must not warn because the
      rest of the corpus is half embedded.
    - More: nothing at all, no figure and so no "partial". That includes a
      scoped API key whose visible set is that large, which must never be
      handed the corpus figure (it describes documents outside its scope).

    A missed warning on a large filtered search is the accepted error; a false
    warning, or a figure from outside the caller's scope, is not.
    """
    if scope_doc_ids is None:
        return await corpus_embedding_coverage(session)
    if len(scope_doc_ids) <= EXACT_COVERAGE_MAX_DOCS:
        return await documents_embedding_coverage(session, scope_doc_ids)
    return None


def reset() -> None:
    """Forget the cached figure (tests)."""
    global _cache, _refreshing
    _cache = None
    _refreshing = False
