"""Hybrid search engine — FTS + vector merge with score normalization."""

import datetime as dt
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.search_rerank import rerank_hits
from harbor_clerk.search_types import ConflictSource, FindAllHit, FindAllResult, SearchHit, SearchResult

# Re-export for backward compatibility — callers that do
# `from harbor_clerk.search import SearchHit` continue to work.
__all__ = [
    "ConflictSource",
    "FindAllHit",
    "FindAllResult",
    "SearchHit",
    "SearchResult",
    "_apply_email_metadata_filter",
    "_apply_jsonb_metadata_filter",
    "find_all",
    "hybrid_search",
]

logger = logging.getLogger(__name__)

# Max docs returned when presentation="full" — keeps payload under ~20 KB
# (top chunk text per doc, ~500 chars).
_FIND_ALL_FULL_MAX_RESULTS = 30

_PROCESSING_PIPELINE_STATUSES: tuple[PipelineStatus, ...] = (
    PipelineStatus.queued,
    PipelineStatus.extracting,
    PipelineStatus.extracted,
    PipelineStatus.ocr_running,
    PipelineStatus.ocr_done,
    PipelineStatus.chunking,
    PipelineStatus.chunked,
    PipelineStatus.extracting_entities,
    PipelineStatus.entities_done,
    PipelineStatus.embedding,
    PipelineStatus.embedded,
    PipelineStatus.summarizing,
    PipelineStatus.summarized,
    PipelineStatus.finalizing,
)


async def _embed_query(query: str) -> list[float]:
    """Embed a query string via the embedder service."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.embedder_url}/embed",
            json={"texts": [query], "task": "query"},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


def _apply_email_metadata_filter(
    metadata_filter: dict[str, Any],
    doc_conditions: list,
) -> dict[str, Any]:
    """Pre-pass for metadata_filter: strip email.* keys, build column predicates,
    append to doc_conditions, return the remaining non-email.* keys for downstream
    JSONB processing.

    The Document.email_* columns are dedicated typed columns, not stored in
    doc_metadata JSONB, so they need column-level predicates rather than
    JSONB containment. This helper is called by both hybrid_search() and
    _query_documents_by_date() so the dispatch logic stays in one place.

    Raises ValueError on unknown email.* keys.
    """
    from sqlalchemy import false, func, or_

    from harbor_clerk.sql_escape import escape_ilike

    email_keys = {k: v for k, v in metadata_filter.items() if k.startswith("email.")}
    if not email_keys:
        return metadata_filter

    remaining = {k: v for k, v in metadata_filter.items() if not k.startswith("email.")}

    for path, value in email_keys.items():
        _, _, subkey = path.partition(".")
        if subkey == "from_address":
            doc_conditions.append(func.lower(Document.email_from_address) == value.lower())
        elif subkey == "from_name":
            doc_conditions.append(Document.email_from_name == value)
        elif subkey == "from_name_contains":
            escaped = escape_ilike(value)
            doc_conditions.append(Document.email_from_name.ilike(f"%{escaped}%", escape="\\"))
        elif subkey == "subject":
            doc_conditions.append(Document.email_subject == value)
        elif subkey == "subject_contains":
            escaped = escape_ilike(value)
            doc_conditions.append(Document.email_subject.ilike(f"%{escaped}%", escape="\\"))
        elif subkey == "to_addresses":
            from sqlalchemy import any_

            if isinstance(value, list):
                if not value:
                    # Empty list explicitly matches nothing (avoid or_(*[]) silently
                    # dropping the WHERE clause and matching every doc).
                    doc_conditions.append(false())
                else:
                    doc_conditions.append(or_(*[v == any_(Document.email_to_addresses) for v in value]))
            else:
                doc_conditions.append(value == any_(Document.email_to_addresses))
        elif subkey == "cc_addresses":
            from sqlalchemy import any_

            if isinstance(value, list):
                if not value:
                    # Empty list explicitly matches nothing (avoid or_(*[]) silently
                    # dropping the WHERE clause and matching every doc).
                    doc_conditions.append(false())
                else:
                    doc_conditions.append(or_(*[v == any_(Document.email_cc_addresses) for v in value]))
            else:
                doc_conditions.append(value == any_(Document.email_cc_addresses))
        elif subkey == "thread_id":
            doc_conditions.append(Document.email_thread_id == value)
        elif subkey == "message_id":
            doc_conditions.append(Document.email_message_id == value)
        else:
            raise ValueError(
                f"unknown email.* filter key: {path!r}. Recognized: "
                f"email.from_address, email.from_name, email.from_name_contains, "
                f"email.subject, email.subject_contains, email.to_addresses, "
                f"email.cc_addresses, email.thread_id, email.message_id."
            )

    return remaining


def _apply_jsonb_metadata_filter(
    metadata_filter: dict[str, Any],
    doc_conditions: list,
) -> None:
    """Translate non-email.* metadata_filter keys into JSONB predicates.

    Each `<namespace>.<key>: value` pair becomes either:
      - JSONB @> containment: doc_metadata @> '{"ns": {"key": value}}'
        (matches scalar metadata)
      - OR JSONB ? existence: doc_metadata->'ns'->'key' ? 'value'
        (matches list-valued metadata containing the scalar)

    The OR lets a caller use a scalar filter value to match either a
    scalar metadata field (sidecar.vendor: "Acme") OR a list-valued one
    (frontmatter.tags: ["alpha", "beta"]) without knowing the shape.
    Only string filter values get the OR — JSONB `?` operator requires
    string keys, so non-string scalars (numbers, bools) use containment
    only.

    Mutates doc_conditions in place by appending predicates; returns
    None. Companion to _apply_email_metadata_filter — that one strips
    email.* keys and returns the remaining dict; this one consumes
    that remaining dict.

    Raises ValueError on malformed keys (must be 'namespace.key', one
    dot, two non-empty segments). Nested paths are not supported in v1.
    """
    for path, value in metadata_filter.items():
        if path.count(".") != 1:
            raise ValueError(
                f"metadata_filter keys must be exactly 'namespace.key' (one dot, two segments); "
                f"got {path!r}. Nested paths are not supported in v1."
            )
        ns, _, key = path.partition(".")
        if not ns or not key:
            raise ValueError(f"metadata_filter keys must have a non-empty namespace and key, got {path!r}")
        containment = Document.doc_metadata.op("@>")(func.cast({ns: {key: value}}, JSONB))
        if isinstance(value, str):
            existence = Document.doc_metadata[ns][key].op("?")(value)
            doc_conditions.append(or_(containment, existence))
        else:
            doc_conditions.append(containment)


def _normalize_scores(scores: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
    """Normalize scores to 0-1 range within a candidate set."""
    if not scores:
        return scores
    max_score = max(scores.values())
    min_score = min(scores.values())
    spread = max_score - min_score
    if spread == 0:
        return {k: 1.0 for k in scores}
    return {k: (v - min_score) / spread for k, v in scores.items()}


def _parse_pipeline_status_filter(value: str | None) -> list[PipelineStatus]:
    """Parse UI/REST pipeline status filters.

    Accepts concrete PipelineStatus enum values plus the friendly "processing"
    group used by the Search Workbench.
    """
    if not value:
        return []

    statuses: list[PipelineStatus] = []
    seen: set[PipelineStatus] = set()
    for raw_status in value.split(","):
        raw_status = raw_status.strip()
        if not raw_status:
            continue
        if raw_status == "processing":
            candidates = list(_PROCESSING_PIPELINE_STATUSES)
        else:
            try:
                candidates = [PipelineStatus(raw_status)]
            except ValueError as exc:
                allowed = ", ".join([s.value for s in PipelineStatus] + ["processing"])
                raise ValueError(f"Invalid pipeline_status {raw_status!r}. Expected one of: {allowed}") from exc
        for status in candidates:
            if status not in seen:
                seen.add(status)
                statuses.append(status)
    return statuses


def _current_job_exists(
    *,
    stage: JobStage | None = None,
    statuses: tuple[JobStatus, ...] = (),
    reason: str | None = None,
) -> Any:
    conditions = [
        IngestionJob.doc_id == Document.doc_id,
        IngestionJob.pipeline_seq == Document.pipeline_seq,
    ]
    if stage is not None:
        conditions.append(IngestionJob.stage == stage)
    if statuses:
        conditions.append(IngestionJob.status.in_(statuses))
    if reason is not None:
        conditions.append(IngestionJob.metrics["reason"].astext == reason)
    return select(IngestionJob.job_id).where(*conditions).exists()


def _summary_state_condition(summary_state: str) -> Any:
    if summary_state == "has":
        return func.length(func.trim(func.coalesce(Document.summary, ""))) > 0
    if summary_state == "missing":
        return func.length(func.trim(func.coalesce(Document.summary, ""))) == 0
    if summary_state == "failed":
        return _current_job_exists(stage=JobStage.summarize, statuses=(JobStatus.error,))
    if summary_state == "pending":
        return _current_job_exists(stage=JobStage.summarize, statuses=(JobStatus.queued, JobStatus.running))
    raise ValueError("Invalid summary_state. Expected one of: has, missing, failed, pending")


def _status_cleanup_condition() -> Any:
    return (
        select(IngestionJob.job_id)
        .where(
            IngestionJob.doc_id == Document.doc_id,
            IngestionJob.pipeline_seq == Document.pipeline_seq,
            IngestionJob.stage == JobStage.finalize,
            IngestionJob.status == JobStatus.done,
            Document.pipeline_status.in_(_PROCESSING_PIPELINE_STATUSES),
        )
        .exists()
    )


def _job_issue_condition(job_issue: str) -> Any:
    failed_job = _current_job_exists(statuses=(JobStatus.error,))
    ocr_failed = _current_job_exists(stage=JobStage.ocr, statuses=(JobStatus.error,))
    entity_skipped = _current_job_exists(stage=JobStage.entities, reason="spacy_unavailable")
    summary_failed = _current_job_exists(stage=JobStage.summarize, statuses=(JobStatus.error,))
    summary_blocked = _current_job_exists(
        stage=JobStage.summarize,
        statuses=(JobStatus.queued, JobStatus.running),
        reason="apple_intelligence_unavailable",
    )
    summary_pending = _current_job_exists(stage=JobStage.summarize, statuses=(JobStatus.queued, JobStatus.running))
    status_cleanup = _status_cleanup_condition()

    if job_issue == "any_issue":
        return or_(
            Document.pipeline_status == PipelineStatus.error,
            failed_job,
            entity_skipped,
            summary_failed,
            summary_blocked,
            status_cleanup,
        )
    if job_issue == "failed_job":
        return failed_job
    if job_issue == "ocr_failed":
        return ocr_failed
    if job_issue == "entity_skipped":
        return entity_skipped
    if job_issue == "summary_failed":
        return summary_failed
    if job_issue == "summary_blocked":
        return summary_blocked
    if job_issue == "summary_pending":
        return summary_pending
    if job_issue == "status_cleanup":
        return status_cleanup

    raise ValueError(
        "Invalid job_issue. Expected one of: any_issue, failed_job, ocr_failed, entity_skipped, "
        "summary_failed, summary_blocked, summary_pending, status_cleanup"
    )


def _apply_document_state_filters(
    doc_conditions: list,
    *,
    pipeline_status: str | None = None,
    summary_state: str | None = None,
    job_stage: str | None = None,
    job_status: str | None = None,
    job_issue: str | None = None,
) -> None:
    statuses = _parse_pipeline_status_filter(pipeline_status)
    if statuses:
        doc_conditions.append(Document.pipeline_status.in_(statuses))

    if summary_state:
        doc_conditions.append(_summary_state_condition(summary_state))

    if job_stage or job_status:
        stage = JobStage(job_stage) if job_stage else None
        statuses_tuple = (JobStatus(job_status),) if job_status else ()
        doc_conditions.append(_current_job_exists(stage=stage, statuses=statuses_tuple))

    if job_issue:
        doc_conditions.append(_job_issue_condition(job_issue))


async def hybrid_search(
    session: AsyncSession,
    query: str,
    k: int = 10,
    doc_id: uuid.UUID | None = None,
    offset: int = 0,
    *,
    doc_ids: list[uuid.UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
    text_contains: str | None = None,
    summary_state: str | None = None,
    pipeline_status: str | None = None,
    job_stage: str | None = None,
    job_status: str | None = None,
    job_issue: str | None = None,
    bypass_reranker: bool = False,
) -> SearchResult:
    """Run hybrid FTS + vector search, merge scores, return top K."""

    if doc_id is not None and doc_ids is not None:
        raise ValueError("Cannot specify both doc_id and doc_ids")

    # --- Scope filters ---
    scope_filters = []
    if doc_id is not None:
        scope_filters.append(Chunk.doc_id == doc_id)
    if doc_ids is not None:
        scope_filters.append(Chunk.doc_id.in_(doc_ids))
    if language is not None:
        scope_filters.append(Chunk.language == language)
    if text_contains is not None:
        from harbor_clerk.sql_escape import escape_ilike

        escaped = escape_ilike(text_contains)
        scope_filters.append(Chunk.chunk_text.ilike(f"%{escaped}%", escape="\\"))

    # Document-level filters — subquery on doc_ids
    doc_conditions = []
    if after is not None:
        doc_conditions.append(Document.created_at >= after)
    if before is not None:
        doc_conditions.append(Document.created_at < before)
    if mime_type is not None:
        doc_conditions.append(Document.mime_type == mime_type)
    _apply_document_state_filters(
        doc_conditions,
        pipeline_status=pipeline_status,
        summary_state=summary_state,
        job_stage=job_stage,
        job_status=job_status,
        job_issue=job_issue,
    )
    # Metadata filter translation. Each "<namespace>.<key>": value pair
    # becomes either:
    #   - JSONB @> containment: doc_metadata @> '{"ns": {"key": value}}'
    #     (matches scalar metadata)
    #   - OR JSONB ? existence: doc_metadata->'ns'->'key' ? 'value'
    #     (matches list-valued metadata containing the scalar)
    # The OR lets a caller use a scalar filter value to match either a
    # scalar metadata field (sidecar.vendor: "Acme") OR a list-valued one
    # (frontmatter.tags: ["alpha", "beta"]) without knowing the shape.
    # Only string filter values get the OR — JSONB `?` operator requires
    # string keys, so non-string scalars (numbers, bools) use containment
    # only.
    # --- email.* pre-pass: strip email.* keys before the JSONB translator ---
    # The Document.email_* columns are dedicated typed columns, not in
    # doc_metadata JSONB, so they need column-level predicates rather than
    # JSONB containment. We pull these out first, build predicates, and
    # let the remaining keys (if any) flow through the JSONB block below.
    if metadata_filter:
        metadata_filter = _apply_email_metadata_filter(metadata_filter, doc_conditions)

    if metadata_filter:
        _apply_jsonb_metadata_filter(metadata_filter, doc_conditions)

    if doc_conditions:
        doc_subq = select(Document.doc_id).where(*doc_conditions)
        scope_filters.append(Chunk.doc_id.in_(doc_subq))

    # Dynamic candidate limit: pull enough candidates for k + offset
    candidate_limit = max(30, k + offset + 20)

    # --- 1. Lexical candidates (FTS) ---
    fts_scores: dict[uuid.UUID, float] = {}

    for lang, col in [("english", Chunk.fts_en), ("french", Chunk.fts_fr)]:
        tsquery = func.websearch_to_tsquery(lang, query)
        rank = func.ts_rank_cd(col, tsquery)
        stmt = (
            select(Chunk.chunk_id, rank.label("rank"))
            .where(col.op("@@")(tsquery), *scope_filters)
            .order_by(rank.desc())
            .limit(candidate_limit)
        )
        result = await session.execute(stmt)
        for row in result:
            cid = row.chunk_id
            if cid not in fts_scores or row.rank > fts_scores[cid]:
                fts_scores[cid] = float(row.rank)

    # --- 2. Semantic candidates (vector) ---
    vector_scores: dict[uuid.UUID, float] = {}
    try:
        query_embedding = await _embed_query(query)
        distance = Chunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(Chunk.chunk_id, distance.label("distance"))
            .where(Chunk.embedding.isnot(None), *scope_filters)
            .order_by(distance)
            .limit(candidate_limit)
        )
        result = await session.execute(stmt)
        for row in result:
            vector_scores[row.chunk_id] = 1.0 - float(row.distance)
    except Exception:
        logger.warning("Embedder unavailable, falling back to lexical-only search")

    # --- 3. Normalize & merge ---
    norm_fts = _normalize_scores(fts_scores)
    norm_vec = _normalize_scores(vector_scores)

    all_chunk_ids = set(norm_fts) | set(norm_vec)
    if not all_chunk_ids:
        return SearchResult(hits=[])

    combined: dict[uuid.UUID, float] = {}
    for cid in all_chunk_ids:
        combined[cid] = norm_fts.get(cid, 0.0) + norm_vec.get(cid, 0.0)

    # --- 4. Load chunk data + apply boosts ---
    chunk_ids_list = list(all_chunk_ids)
    chunks_result = await session.execute(select(Chunk).where(Chunk.chunk_id.in_(chunk_ids_list)))
    chunks_by_id: dict[uuid.UUID, Chunk] = {c.chunk_id: c for c in chunks_result.scalars().all()}

    # Load documents for titles and OCR-confidence boost
    doc_ids_set = {c.doc_id for c in chunks_by_id.values()}
    docs_result = await session.execute(select(Document).where(Document.doc_id.in_(list(doc_ids_set))))
    docs_by_id: dict[uuid.UUID, Document] = {d.doc_id: d for d in docs_result.scalars().all()}

    for cid, score in combined.items():
        chunk = chunks_by_id.get(cid)
        if chunk is None:
            continue
        # Boost for OCR confidence
        if chunk.ocr_confidence is not None:
            score += 0.05 * (chunk.ocr_confidence / 100.0)
        combined[cid] = score

    # --- 5. Rank, optionally rerank, take the requested page ---
    total_candidates = len(combined)
    sorted_ids = sorted(combined, key=lambda cid: combined[cid], reverse=True)

    def _build_hit(cid):
        chunk = chunks_by_id.get(cid)
        if chunk is None:
            return None
        doc = docs_by_id.get(chunk.doc_id)
        return SearchHit(
            chunk_id=str(cid),
            doc_id=str(chunk.doc_id),
            chunk_num=chunk.chunk_num,
            chunk_text=chunk.chunk_text,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            language=chunk.language,
            ocr_used=chunk.ocr_used,
            ocr_confidence=chunk.ocr_confidence,
            score=round(combined[cid], 4),
            doc_title=doc.title if doc else None,
        )

    settings = get_settings()
    reranker_status: str = "disabled"

    if settings.reranker_enabled and not bypass_reranker:
        # Rerank a candidate pool spanning the requested page (offset + k),
        # padded for rerank headroom and capped by reranker_pool_size, then
        # slice the page out of the reranked order.
        want = offset + k
        pool_size = min(want + settings.reranker_top_k_pad, settings.reranker_pool_size)
        pool_hits = [h for h in (_build_hit(cid) for cid in sorted_ids[:pool_size]) if h is not None]
        if pool_hits:
            # Cap reported total to the servable depth — we cannot page past the
            # pool, so has_more must not signal pages that will always be empty.
            total_candidates = len(pool_hits)
            reranked, reranker_status = await rerank_hits(query, pool_hits, top_k=want, return_status=True)
            # On reranker failure rerank_hits returns status "failed"; fall back
            # to the hybrid-sorted pool so search still works.
            ranked = reranked if reranker_status == "ok" else pool_hits
            hits = ranked[offset : offset + k]
        else:
            total_candidates = 0
            hits = []
    else:
        hits = [h for h in (_build_hit(cid) for cid in sorted_ids[offset : offset + k]) if h is not None]

    # --- 6. Conflict detection ---
    possible_conflict = False
    conflict_sources: list[ConflictSource] = []
    if len(hits) >= 2:
        top3 = hits[:3]
        top_score = top3[0].score
        threshold = top_score * 0.9  # within 10%
        close_hits = [h for h in top3 if h.score >= threshold]
        unique_docs = {h.doc_id for h in close_hits}
        if len(unique_docs) > 1:
            possible_conflict = True
            for doc_id_str in unique_docs:
                doc = docs_by_id.get(uuid.UUID(doc_id_str))
                conflict_sources.append(
                    ConflictSource(
                        doc_id=doc_id_str,
                        title=doc.title if doc else "Unknown",
                    )
                )

    return SearchResult(
        hits=hits,
        total_candidates=total_candidates,
        possible_conflict=possible_conflict,
        conflict_sources=conflict_sources,
        reranker_status=reranker_status,
    )


async def find_all(
    session: AsyncSession,
    query: str,
    *,
    max_results: int = 100,
    offset: int = 0,
    sort_by: str = "relevance",
    text_contains: str | None = None,
    doc_id: uuid.UUID | None = None,
    doc_ids: list[uuid.UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
    summary_state: str | None = None,
    pipeline_status: str | None = None,
    job_stage: str | None = None,
    job_status: str | None = None,
    job_issue: str | None = None,
    presentation: str = "brief",
) -> FindAllResult:
    """Doc-level enumeration. Aggregates chunks → docs, sorts, paginates.

    Reuses hybrid_search for candidate generation, then projects chunk-level
    hits to doc-level with score = max(chunk_score per doc). See
    docs/superpowers/specs/2026-05-26-find-iteration-enumeration-design.md.
    """
    _VALID_SORTS = {"relevance", "date_desc", "date_asc"}
    if sort_by not in _VALID_SORTS:
        raise ValueError(f"sort_by must be one of {sorted(_VALID_SORTS)}, got {sort_by!r}")

    _VALID_PRESENTATIONS = {"brief", "full"}
    if presentation not in _VALID_PRESENTATIONS:
        raise ValueError(f"presentation must be one of {sorted(_VALID_PRESENTATIONS)}, got {presentation!r}")

    # internal_k uses the ORIGINAL max_results so total_matches stays accurate
    # even when the return slice is clamped by presentation="full".
    # Pull a generous candidate pool so dedupe doesn't starve the result set
    # AND high-offset pagination reports accurate total_matches.
    # Internal k = 5x (offset + max_results), capped at 1000.
    internal_k = min((offset + max_results) * 5, 1000)

    # Window size — clamped for presentation="full" to bound the payload.
    if presentation == "full" and max_results > _FIND_ALL_FULL_MAX_RESULTS:
        return_limit = _FIND_ALL_FULL_MAX_RESULTS
    else:
        return_limit = max_results

    # Enumeration must operate on the full FTS+vector candidate pool. The
    # reranker is a chunk-level precision tool: when enabled, hybrid_search
    # caps its result pool at settings.reranker_pool_size (default 50),
    # which silently truncates total_matches. For find_all that breaks the
    # enumeration contract — we want broad coverage, not top-K precision.
    inner = await hybrid_search(
        session,
        query,
        k=internal_k,
        offset=0,
        doc_id=doc_id,
        doc_ids=doc_ids,
        after=after,
        before=before,
        language=language,
        mime_type=mime_type,
        metadata_filter=metadata_filter,
        text_contains=text_contains,
        summary_state=summary_state,
        pipeline_status=pipeline_status,
        job_stage=job_stage,
        job_status=job_status,
        job_issue=job_issue,
        bypass_reranker=True,  # enumeration must see the full candidate pool
    )

    # Group hits by doc_id (str); keep the max-score chunk per doc.
    by_doc: dict[str, SearchHit] = {}
    for hit in inner.hits:
        existing = by_doc.get(hit.doc_id)
        if existing is None or hit.score > existing.score:
            by_doc[hit.doc_id] = hit

    total_matches = len(by_doc)

    # Sort the doc-level dict. For date sorts, fetch created_at up front.
    if sort_by in ("date_desc", "date_asc"):
        date_rows = (
            await session.execute(
                select(Document.doc_id, Document.created_at).where(Document.doc_id.in_([uuid.UUID(k) for k in by_doc]))
            )
        ).all()
        # Key by str to match by_doc keys
        date_map: dict[str, datetime] = {str(r.doc_id): r.created_at for r in date_rows}
        docs_sorted = sorted(
            by_doc.values(),
            key=lambda h: date_map.get(h.doc_id, dt.datetime.min.replace(tzinfo=dt.UTC)),
            reverse=(sort_by == "date_desc"),
        )
    else:  # relevance (default)
        docs_sorted = sorted(by_doc.values(), key=lambda h: h.score, reverse=True)

    # Slice offset..offset+return_limit
    window = docs_sorted[offset : offset + return_limit]

    # Build FindAllHit rows — needs Document.mime_type / created_at, which
    # SearchHit doesn't carry. Fetch in one round-trip.
    # SearchHit.doc_id is a str; cast to uuid.UUID for the SQL IN() clause.
    if window:
        doc_id_list = [uuid.UUID(h.doc_id) for h in window]
        rows = (
            await session.execute(
                select(Document.doc_id, Document.title, Document.mime_type, Document.created_at).where(
                    Document.doc_id.in_(doc_id_list)
                )
            )
        ).all()
        doc_meta = {str(r.doc_id): r for r in rows}
    else:
        doc_meta = {}

    hits: list[FindAllHit] = []
    for sh in window:
        meta = doc_meta.get(sh.doc_id)
        if meta is None:
            continue
        hits.append(
            FindAllHit(
                doc_id=sh.doc_id,
                doc_title=meta.title or sh.doc_title or "",
                mime_type=meta.mime_type or "",
                language=sh.language,
                score=sh.score,
                ingested_at=meta.created_at,
                page_range=(
                    f"{sh.page_start}-{sh.page_end}"
                    if sh.page_start is not None and sh.page_end is not None
                    else (str(sh.page_start) if sh.page_start is not None else None)
                ),
                top_chunk_id=sh.chunk_id if presentation == "full" else None,
                top_chunk_text=sh.chunk_text if presentation == "full" else None,
                top_chunk_page=sh.page_start if presentation == "full" else None,
                top_chunk_heading=None,  # SearchHit has no heading field
            )
        )

    return FindAllResult(
        hits=hits,
        total_matches=total_matches,
        offset=offset,
        truncated=total_matches > offset + len(hits),
        sort_by=sort_by,
        presentation=presentation,
    )
