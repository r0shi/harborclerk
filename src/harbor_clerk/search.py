"""Hybrid search engine — FTS + vector merge with score normalization."""

import logging
import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings
from harbor_clerk.models import Chunk, Document

logger = logging.getLogger(__name__)


@dataclass
class ConflictSource:
    doc_id: str
    version_id: str
    title: str


@dataclass
class SearchHit:
    chunk_id: str
    doc_id: str
    version_id: str
    chunk_num: int
    chunk_text: str
    page_start: int | None
    page_end: int | None
    language: str
    ocr_used: bool
    ocr_confidence: float | None
    score: float
    doc_title: str | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total_candidates: int = 0
    possible_conflict: bool = False
    conflict_sources: list[ConflictSource] = field(default_factory=list)


async def _embed_query(query: str) -> list[float]:
    """Embed a query string via the embedder service."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.embedder_url}/embed",
            json={"texts": [query]},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


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


async def hybrid_search(
    session: AsyncSession,
    query: str,
    k: int = 10,
    doc_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    offset: int = 0,
) -> SearchResult:
    """Run hybrid FTS + vector search, merge scores, return top K."""

    # --- Scope filters ---
    scope_filters = []
    if doc_id is not None:
        scope_filters.append(Chunk.doc_id == doc_id)
    if version_id is not None:
        scope_filters.append(Chunk.version_id == version_id)

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
    chunks_result = await session.execute(
        select(Chunk).where(Chunk.chunk_id.in_(chunk_ids_list))
    )
    chunks_by_id: dict[uuid.UUID, Chunk] = {
        c.chunk_id: c for c in chunks_result.scalars().all()
    }

    # Load documents for latest_version_id check and titles
    doc_ids = {c.doc_id for c in chunks_by_id.values()}
    docs_result = await session.execute(
        select(Document).where(Document.doc_id.in_(list(doc_ids)))
    )
    docs_by_id: dict[uuid.UUID, Document] = {
        d.doc_id: d for d in docs_result.scalars().all()
    }

    for cid, score in combined.items():
        chunk = chunks_by_id.get(cid)
        if chunk is None:
            continue
        doc = docs_by_id.get(chunk.doc_id)
        # Boost for latest version
        if doc and doc.latest_version_id == chunk.version_id:
            score += 0.1
        # Boost for OCR confidence
        if chunk.ocr_confidence is not None:
            score += 0.05 * (chunk.ocr_confidence / 100.0)
        combined[cid] = score

    # --- 5. Top K with offset ---
    total_candidates = len(combined)
    sorted_ids = sorted(combined, key=lambda cid: combined[cid], reverse=True)
    sorted_ids = sorted_ids[offset:offset + k]

    hits: list[SearchHit] = []
    for cid in sorted_ids:
        chunk = chunks_by_id.get(cid)
        if chunk is None:
            continue
        doc = docs_by_id.get(chunk.doc_id)
        hits.append(SearchHit(
            chunk_id=str(cid),
            doc_id=str(chunk.doc_id),
            version_id=str(chunk.version_id),
            chunk_num=chunk.chunk_num,
            chunk_text=chunk.chunk_text,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            language=chunk.language,
            ocr_used=chunk.ocr_used,
            ocr_confidence=chunk.ocr_confidence,
            score=round(combined[cid], 4),
            doc_title=doc.title if doc else None,
        ))

    # --- 6. Conflict detection ---
    possible_conflict = False
    conflict_sources: list[ConflictSource] = []
    if len(hits) >= 2:
        top3 = hits[:3]
        top_score = top3[0].score
        threshold = top_score * 0.9  # within 10%
        close_hits = [h for h in top3 if h.score >= threshold]
        unique_sources = {(h.doc_id, h.version_id) for h in close_hits}
        if len(unique_sources) > 1:
            possible_conflict = True
            for doc_id_str, version_id_str in unique_sources:
                doc = docs_by_id.get(uuid.UUID(doc_id_str))
                conflict_sources.append(ConflictSource(
                    doc_id=doc_id_str,
                    version_id=version_id_str,
                    title=doc.title if doc else "Unknown",
                ))

    return SearchResult(
        hits=hits,
        total_candidates=total_candidates,
        possible_conflict=possible_conflict,
        conflict_sources=conflict_sources,
    )
