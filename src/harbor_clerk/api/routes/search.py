"""Search and passage-reading endpoints."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.api.schemas.search import (
    ConflictSourceOut,
    PassageDetail,
    ReadPassagesRequest,
    ReadPassagesResponse,
    SearchHitOut,
    SearchRequest,
    SearchResponse,
)
from harbor_clerk.db import get_session
from harbor_clerk.models import Chunk, Document
from harbor_clerk.search import hybrid_search

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    doc_id = uuid.UUID(body.doc_id) if body.doc_id else None
    version_id = uuid.UUID(body.version_id) if body.version_id else None

    result = await hybrid_search(
        session, body.query, k=body.k, doc_id=doc_id, version_id=version_id,
    )

    return SearchResponse(
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id,
                doc_id=h.doc_id,
                version_id=h.version_id,
                chunk_num=h.chunk_num,
                chunk_text=h.chunk_text,
                page_start=h.page_start,
                page_end=h.page_end,
                language=h.language,
                ocr_used=h.ocr_used,
                ocr_confidence=h.ocr_confidence,
                score=h.score,
                doc_title=h.doc_title,
            )
            for h in result.hits
        ],
        possible_conflict=result.possible_conflict,
        conflict_sources=[
            ConflictSourceOut(
                doc_id=cs.doc_id, version_id=cs.version_id, title=cs.title,
            )
            for cs in result.conflict_sources
        ],
    )


@router.post("/passages/read", response_model=ReadPassagesResponse)
async def read_passages(
    body: ReadPassagesRequest,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    chunk_uuids = []
    for cid in body.chunk_ids:
        try:
            chunk_uuids.append(uuid.UUID(cid))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid chunk_id: {cid}",
            )

    result = await session.execute(
        select(Chunk).where(Chunk.chunk_id.in_(chunk_uuids))
    )
    chunks = {c.chunk_id: c for c in result.scalars().all()}

    # Load doc titles
    doc_ids = {c.doc_id for c in chunks.values()}
    docs_result = await session.execute(
        select(Document).where(Document.doc_id.in_(list(doc_ids)))
    )
    docs_by_id = {d.doc_id: d for d in docs_result.scalars().all()}

    # Optionally load surrounding chunks for context
    context_before: dict[uuid.UUID, str] = {}
    context_after: dict[uuid.UUID, str] = {}
    if body.include_context and chunks:
        for chunk in chunks.values():
            # Previous chunk
            prev_result = await session.execute(
                select(Chunk.chunk_text)
                .where(
                    Chunk.version_id == chunk.version_id,
                    Chunk.chunk_num == chunk.chunk_num - 1,
                )
            )
            prev_text = prev_result.scalar_one_or_none()
            if prev_text:
                context_before[chunk.chunk_id] = prev_text

            # Next chunk
            next_result = await session.execute(
                select(Chunk.chunk_text)
                .where(
                    Chunk.version_id == chunk.version_id,
                    Chunk.chunk_num == chunk.chunk_num + 1,
                )
            )
            next_text = next_result.scalar_one_or_none()
            if next_text:
                context_after[chunk.chunk_id] = next_text

    # Preserve request order
    passages = []
    for cid in chunk_uuids:
        chunk = chunks.get(cid)
        if chunk is None:
            continue
        doc = docs_by_id.get(chunk.doc_id)
        passages.append(PassageDetail(
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
            doc_title=doc.title if doc else None,
            context_before=context_before.get(cid),
            context_after=context_after.get(cid),
        ))

    return ReadPassagesResponse(passages=passages)
