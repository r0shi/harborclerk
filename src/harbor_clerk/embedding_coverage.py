"""How much of the corpus semantic search can see: the share of active documents' chunks with a vector.

A chunk without an embedding is invisible to the vector leg; after #685 a
failing night left 29% of a corpus without one, and the only way to learn it
was to query Postgres. One aggregate serves both readers:

- `/system/stats` reports the counts (`embedding_coverage_counts`), uncached;
- search marks a result "partial" from the figure for its reach
  (`search_scope_coverage`): with no document filter, the corpus share
  (`corpus_embedding_coverage`), through a process-wide cache refreshed in the
  background at most once per CORPUS_COVERAGE_TTL_SECONDS. The aggregate is not
  free (measured at 125 ms warm and 4.1 s cold on 300k chunks), so no search
  pays it once a figure exists: a stale figure is served while a background
  task replaces it, and only a process's first search waits, for at most
  FIRST_FIGURE_WAIT_SECONDS.

Not over a query's candidates. That figure was diluted by the vector leg itself
(it only ever returns embedded chunks), so a corpus with a third of its
documents unembedded could read fully covered, and it scanned every chunk of up
to ~1,000 candidate documents per find_all.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from harbor_clerk.models import Chunk, Document

logger = logging.getLogger(__name__)

# How long a corpus coverage figure stands before the next search refreshes it.
# Coverage moves as ingest embeds documents, minutes not seconds; one aggregate
# a minute keeps the cost off the search path.
CORPUS_COVERAGE_TTL_SECONDS = 60.0

# How long the first search in a process waits for the first figure. Warm, the
# aggregate takes ~125 ms; cold, seconds. Past this the search reports no figure
# (a missed warning, the accepted error) and the refresh finishes in the background.
FIRST_FIGURE_WAIT_SECONDS = 2.0

# After a failed refresh, how long before another is tried: a database that just
# failed the aggregate should not be asked again on every search.
CORPUS_COVERAGE_RETRY_SECONDS = 30.0

# A document filter this small gets an exact figure over just its documents:
# one aggregate on chunks.doc_id (chunks_doc_idx), bounded by the filter. 50 is
# the doc_ids cap REST and MCP already enforce on a caller's own filter.
EXACT_COVERAGE_MAX_DOCS = 50

_cache: tuple[float, float | None] | None = None  # (monotonic time, share)
# (loop, task) of the in-flight (or last) refresh: a task belongs to its loop.
_refresh: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = None
_failed_at: float | None = None


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
    """The share of active chunks with a vector (None when unknown or the corpus is empty), cached process-wide.

    Never pays the aggregate on the search path once a figure exists:

    - fresh figure: returned;
    - stale figure: returned at once, and one background refresh started
      (single-flight: a refresh already running is reused, not doubled);
    - no figure yet (the first search in a process): the refresh is started
      and awaited for at most FIRST_FIGURE_WAIT_SECONDS; past that this search
      reports no figure and the refresh finishes in the background.

    The refresh runs on its own session from the caller's engine, since it may
    outlive the caller's request. A failed refresh is logged and keeps the
    previous figure, and is not retried for CORPUS_COVERAGE_RETRY_SECONDS.
    """
    cached = _cache
    if cached is not None and _now() - cached[0] < CORPUS_COVERAGE_TTL_SECONDS:
        return cached[1]
    task = _start_refresh(session)
    if cached is not None:
        return cached[1]
    if task is None:
        return None  # a recent refresh failed; don't make every first search wait on another
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=FIRST_FIGURE_WAIT_SECONDS)
    except TimeoutError:
        return None
    latest = _cache
    return latest[1] if latest is not None else None


def _refresh_task() -> asyncio.Task | None:
    """The in-flight (or last) refresh on the running loop, if any (tests await it)."""
    refresh = _refresh
    if refresh is None or refresh[0] is not asyncio.get_running_loop():
        return None
    return refresh[1]


def _start_refresh(session: AsyncSession) -> asyncio.Task | None:
    """Start the background refresh unless one is running (returned) or one just failed (None)."""
    global _refresh
    running = _refresh_task()
    if running is not None and not running.done():
        return running
    if _failed_at is not None and _now() - _failed_at < CORPUS_COVERAGE_RETRY_SECONDS:
        return None
    bind = session.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    if engine is None:
        from harbor_clerk.db import engine
    loop = asyncio.get_running_loop()
    # Held in _refresh: the loop keeps only a weak reference to a task, and an
    # unreferenced one can be garbage-collected mid-run.
    _refresh = (loop, loop.create_task(_refresh_figure(engine)))
    return _refresh[1]


async def _refresh_figure(engine) -> None:
    """Run the aggregate on its own session and cache the share; log and swallow any failure."""
    global _cache, _failed_at
    try:
        async with AsyncSession(bind=engine) as refresh_session:
            counts = await embedding_coverage_counts(refresh_session)
        total = counts["chunks_total"]
        share = round(counts["chunks_embedded"] / total, 3) if total else None
        _cache = (_now(), share)
        _failed_at = None
    except asyncio.CancelledError:
        raise
    except Exception:
        # Swallowed after logging: nobody awaits a background task's result, so
        # a raised exception would only surface as "Task exception was never
        # retrieved". The previous figure, if any, stands.
        _failed_at = _now()
        logger.exception("Corpus embedding coverage refresh failed; keeping the previous figure")


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

    - No document filter: the cached corpus figure. That includes searches
      with only other filters (date, language, MIME, metadata, pipeline or job
      state): they are not document lists, and they deliberately get the
      corpus figure, so they may over-warn (a date range of fully embedded
      documents in a half-embedded corpus says "partial"). By design: an
      exact figure for them would cost a scan of their matches per search.
    - A document filter of at most EXACT_COVERAGE_MAX_DOCS documents: the
      exact figure over them. A search limited to one fully embedded document
      must not warn because the rest of the corpus is half embedded.
    - A larger document filter: nothing at all, no figure and so no
      "partial". That includes a scoped API key whose visible set is that
      large, which must never be handed the corpus figure (it describes
      documents outside its scope).

    The rule for document filters (doc_id, doc_ids, a scope's visible set):
    never a false warning and never a figure from outside the caller's scope;
    a missed warning on a large one is the accepted error. Other filters use
    the corpus figure and may over-warn.
    """
    if scope_doc_ids is None:
        return await corpus_embedding_coverage(session)
    if len(scope_doc_ids) <= EXACT_COVERAGE_MAX_DOCS:
        # The search already has its results; a failure here costs the
        # coverage figure, never the search. A savepoint, so the failed query
        # does not leave the search's transaction aborted for what follows.
        try:
            async with session.begin_nested():
                return await documents_embedding_coverage(session, scope_doc_ids)
        except Exception:
            logger.exception("Embedding coverage for %d filtered documents failed; reporting none", len(scope_doc_ids))
            return None
    return None


def reset() -> None:
    """Forget the cached figure and cancel any refresh in flight (tests)."""
    global _cache, _refresh, _failed_at
    if _refresh is not None and not _refresh[1].done():
        _refresh[1].cancel()
    _cache = None
    _refresh = None
    _failed_at = None
