"""Search and passage-reading endpoints."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.api.schemas.search import (
    ConflictSourceOut,
    FacetedDocGroup,
    FacetedSearchResponse,
    FindAllHitOut,
    FindAllRequest,
    FindAllResponse,
    FindAllTopChunkOut,
    PassageDetail,
    ReadPassagesRequest,
    ReadPassagesResponse,
    SearchHitOut,
    SearchRequest,
    SearchResponse,
)
from harbor_clerk.api.scope import apply_folder_scope, apply_key_scope
from harbor_clerk.api.scope_validation import validate_scope_folders
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models import Chunk, Document
from harbor_clerk.search import find_all, hybrid_search
from harbor_clerk.search_types import FindAllHit
from harbor_clerk.source_ref import SourceRef, format_document_citation, format_pages, load_source_ref_context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


def _hit_to_out(h, source_ref: SourceRef | None = None) -> SearchHitOut:
    return SearchHitOut(
        chunk_id=h.chunk_id,
        doc_id=h.doc_id,
        chunk_num=h.chunk_num,
        chunk_text=h.chunk_text,
        page_start=h.page_start,
        page_end=h.page_end,
        language=h.language,
        ocr_used=h.ocr_used,
        ocr_confidence=h.ocr_confidence,
        score=h.score,
        doc_title=h.doc_title,
        source=source_ref.to_dict() if source_ref else None,
        citation=source_ref.citation if source_ref else None,
    )


def _fallback_source_ref(
    *,
    doc_id: str,
    doc_title: str | None,
    chunk_id: str | None = None,
    pages: str | None = None,
    section: str | None = None,
) -> SourceRef:
    title = doc_title or "Untitled document"
    return SourceRef(
        doc_id=doc_id,
        doc_title=title,
        chunk_id=chunk_id,
        pages=pages,
        section=section,
        source_kind="unknown",
        source_label=title,
        citation=format_document_citation(title, pages),
    )


def _source_ref_for_find_all_hit(hit: FindAllHit, source_ref: SourceRef | None = None) -> SourceRef:
    if source_ref is not None and source_ref.doc_id:
        return source_ref
    return _fallback_source_ref(
        doc_id=hit.doc_id,
        doc_title=hit.doc_title,
        chunk_id=hit.top_chunk_id,
        pages=hit.page_range,
        section=hit.top_chunk_heading,
    )


def _find_all_hit_to_out(hit: FindAllHit, source_ref: SourceRef | None = None) -> FindAllHitOut:
    resolved_source = _source_ref_for_find_all_hit(hit, source_ref)
    top_chunk = None
    if hit.top_chunk_id is not None or hit.top_chunk_text is not None:
        top_chunk = FindAllTopChunkOut(
            chunk_id=hit.top_chunk_id,
            text=hit.top_chunk_text,
            page=hit.top_chunk_page,
            heading=hit.top_chunk_heading,
        )
    return FindAllHitOut(
        doc_id=hit.doc_id,
        doc_title=hit.doc_title,
        mime_type=hit.mime_type,
        language=hit.language,
        score=hit.score,
        ingested_at=hit.ingested_at,
        page_range=hit.page_range,
        top_chunk=top_chunk,
        source=resolved_source.to_dict(),
        citation=resolved_source.citation,
    )


def _empty_find_all_response(
    *,
    offset: int,
    sort_by: str,
    presentation: str,
) -> FindAllResponse:
    return FindAllResponse(
        results=[],
        total_matches=0,
        returned=0,
        offset=offset,
        truncated=False,
        sort_by=sort_by,
        presentation=presentation,
    )


@router.post("/search", response_model=SearchResponse | FacetedSearchResponse)
async def search(
    body: SearchRequest,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    await validate_scope_folders(body.scope, session)

    doc_id = uuid.UUID(body.doc_id) if body.doc_id else None
    doc_ids = [uuid.UUID(d) for d in body.doc_ids] if body.doc_ids else None

    # Per-API-key scope: intersect with visible doc_ids for scoped keys.
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(select(Document.doc_id), principal)
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}
        if not visible_ids:
            return SearchResponse(
                hits=[],
                total_candidates=0,
                has_more=False,
                possible_conflict=False,
                conflict_sources=[],
            )
        if doc_ids is not None:
            doc_ids = [d for d in doc_ids if d in visible_ids]
            if not doc_ids:
                return SearchResponse(
                    hits=[],
                    total_candidates=0,
                    has_more=False,
                    possible_conflict=False,
                    conflict_sources=[],
                )
        else:
            doc_ids = list(visible_ids)
        # Also enforce single-doc filter against visible set
        if doc_id is not None and doc_id not in visible_ids:
            return SearchResponse(
                hits=[],
                total_candidates=0,
                has_more=False,
                possible_conflict=False,
                conflict_sources=[],
            )

    # Per-request user scope: intersect with visible doc_ids for the requested folders.
    if body.scope is not None and body.scope.folder_ids:
        scoped_q = apply_folder_scope(
            select(Document.doc_id).where(Document.status == "active"),
            list(body.scope.folder_ids),
        )
        scoped_visible = {row[0] for row in (await session.execute(scoped_q)).all()}
        if not scoped_visible:
            return SearchResponse(
                hits=[],
                total_candidates=0,
                has_more=False,
                possible_conflict=False,
                conflict_sources=[],
            )
        if doc_ids is not None:
            doc_ids = [d for d in doc_ids if d in scoped_visible]
            if not doc_ids:
                return SearchResponse(
                    hits=[],
                    total_candidates=0,
                    has_more=False,
                    possible_conflict=False,
                    conflict_sources=[],
                )
        else:
            doc_ids = list(scoped_visible)
        # Also enforce single-doc filter against scoped visible set
        if doc_id is not None and doc_id not in scoped_visible:
            return SearchResponse(
                hits=[],
                total_candidates=0,
                has_more=False,
                possible_conflict=False,
                conflict_sources=[],
            )

    try:
        result = await hybrid_search(
            session,
            body.query,
            k=body.k,
            doc_id=doc_id,
            offset=body.offset,
            doc_ids=doc_ids,
            after=body.after,
            before=body.before,
            language=body.language,
            mime_type=body.mime_type,
            metadata_filter=body.metadata_filter,
            text_contains=body.text_contains,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    has_more = body.offset + body.k < result.total_candidates
    source_context = await load_source_ref_context(session, [h.doc_id for h in result.hits])
    hits_out = [
        _hit_to_out(
            h,
            source_context.ref_for_doc(
                h.doc_id,
                chunk_id=h.chunk_id,
                pages=format_pages(h.page_start, h.page_end),
            ),
        )
        for h in result.hits
    ]

    if body.faceted:
        groups: dict[str, list[SearchHitOut]] = {}
        for h in hits_out:
            groups.setdefault(h.doc_id, []).append(h)
        doc_groups = []
        for did, group_hits in groups.items():
            doc_groups.append(
                FacetedDocGroup(
                    doc_id=did,
                    doc_title=group_hits[0].doc_title,
                    top_score=max(h.score for h in group_hits),
                    hit_count=len(group_hits),
                    hits=group_hits,
                )
            )
        doc_groups.sort(key=lambda g: g.top_score, reverse=True)
        return FacetedSearchResponse(
            documents=doc_groups,
            total_candidates=result.total_candidates,
            has_more=has_more,
        )

    return SearchResponse(
        hits=hits_out,
        total_candidates=result.total_candidates,
        has_more=has_more,
        possible_conflict=result.possible_conflict,
        conflict_sources=[
            ConflictSourceOut(
                doc_id=cs.doc_id,
                title=cs.title,
            )
            for cs in result.conflict_sources
        ],
        reranker_status=result.reranker_status,
    )


@router.post("/search/find-all", response_model=FindAllResponse)
async def search_find_all(
    body: FindAllRequest,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    await validate_scope_folders(body.scope, session)

    doc_id = body.doc_id
    doc_ids = list(body.doc_ids) if body.doc_ids else None
    max_results = max(1, min(body.max_results, get_settings().find_all_max_results_cap))

    # Per-API-key scope: intersect with visible doc_ids for scoped keys.
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(select(Document.doc_id), principal)
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}
        if not visible_ids:
            return _empty_find_all_response(
                offset=body.offset,
                sort_by=body.sort_by,
                presentation=body.presentation,
            )
        if doc_ids is not None:
            doc_ids = [d for d in doc_ids if d in visible_ids]
            if not doc_ids:
                return _empty_find_all_response(
                    offset=body.offset,
                    sort_by=body.sort_by,
                    presentation=body.presentation,
                )
        elif doc_id is None:
            doc_ids = list(visible_ids)
        if doc_id is not None and doc_id not in visible_ids:
            return _empty_find_all_response(
                offset=body.offset,
                sort_by=body.sort_by,
                presentation=body.presentation,
            )

    # Per-request user scope: intersect with visible doc_ids for the requested folders.
    if body.scope is not None and body.scope.folder_ids:
        scoped_q = apply_folder_scope(
            select(Document.doc_id).where(Document.status == "active"),
            list(body.scope.folder_ids),
        )
        scoped_visible = {row[0] for row in (await session.execute(scoped_q)).all()}
        if not scoped_visible:
            return _empty_find_all_response(
                offset=body.offset,
                sort_by=body.sort_by,
                presentation=body.presentation,
            )
        if doc_ids is not None:
            doc_ids = [d for d in doc_ids if d in scoped_visible]
            if not doc_ids:
                return _empty_find_all_response(
                    offset=body.offset,
                    sort_by=body.sort_by,
                    presentation=body.presentation,
                )
        elif doc_id is None:
            doc_ids = list(scoped_visible)
        if doc_id is not None and doc_id not in scoped_visible:
            return _empty_find_all_response(
                offset=body.offset,
                sort_by=body.sort_by,
                presentation=body.presentation,
            )

    try:
        result = await find_all(
            session,
            body.query,
            max_results=max_results,
            offset=body.offset,
            sort_by=body.sort_by,
            text_contains=body.text_contains,
            doc_id=doc_id,
            doc_ids=doc_ids,
            after=body.after,
            before=body.before,
            language=body.language,
            mime_type=body.mime_type,
            metadata_filter=body.metadata_filter,
            presentation=body.presentation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    source_context = await load_source_ref_context(session, [h.doc_id for h in result.hits]) if result.hits else None
    results = []
    for hit in result.hits:
        source_ref = (
            source_context.ref_for_doc(
                hit.doc_id,
                chunk_id=hit.top_chunk_id,
                pages=hit.page_range,
                section=hit.top_chunk_heading,
            )
            if source_context is not None
            else None
        )
        results.append(_find_all_hit_to_out(hit, source_ref))

    return FindAllResponse(
        results=results,
        total_matches=result.total_matches,
        returned=len(results),
        offset=result.offset,
        truncated=result.truncated,
        sort_by=result.sort_by,
        presentation=result.presentation,
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

    # Per-API-key scope: compute the set of visible doc_ids for scoped keys.
    visible_ids: set[uuid.UUID] | None = None
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(select(Document.doc_id), principal)
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}
    elif principal.type == "user" and principal.user_scope is not None and not principal.user_scope.is_unrestricted:
        visible_q = apply_folder_scope(
            select(Document.doc_id).where(Document.status == "active"),
            principal.user_scope.folder_ids,
        )
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}

    result = await session.execute(select(Chunk).where(Chunk.chunk_id.in_(chunk_uuids)))
    chunks = {c.chunk_id: c for c in result.scalars().all()}

    # Drop chunks whose parent doc is outside this key's scope — scoped-out
    # docs must be invisible (same as a 404 on per-doc endpoints).
    if visible_ids is not None:
        chunks = {cid: c for cid, c in chunks.items() if c.doc_id in visible_ids}

    # Load doc titles
    doc_ids = {c.doc_id for c in chunks.values()}
    docs_result = await session.execute(select(Document).where(Document.doc_id.in_(list(doc_ids))))
    docs_by_id = {d.doc_id: d for d in docs_result.scalars().all()}
    source_context = await load_source_ref_context(session, doc_ids)

    # Optionally load surrounding chunks for context
    context_before: dict[uuid.UUID, str] = {}
    context_after: dict[uuid.UUID, str] = {}
    if body.include_context and chunks:
        for chunk in chunks.values():
            # Previous chunk (same doc)
            prev_result = await session.execute(
                select(Chunk.chunk_text).where(
                    Chunk.doc_id == chunk.doc_id,
                    Chunk.chunk_num == chunk.chunk_num - 1,
                )
            )
            prev_text = prev_result.scalar_one_or_none()
            if prev_text:
                context_before[chunk.chunk_id] = prev_text

            # Next chunk (same doc)
            next_result = await session.execute(
                select(Chunk.chunk_text).where(
                    Chunk.doc_id == chunk.doc_id,
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
        source_ref = source_context.ref_for_doc(
            chunk.doc_id,
            chunk_id=cid,
            pages=format_pages(chunk.page_start, chunk.page_end),
        )
        passages.append(
            PassageDetail(
                chunk_id=str(cid),
                doc_id=str(chunk.doc_id),
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
                source=source_ref.to_dict(),
                citation=source_ref.citation,
            )
        )

    return ReadPassagesResponse(passages=passages)
