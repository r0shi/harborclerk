"""Document CRUD endpoints."""

import logging
import posixpath
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin, require_read_access
from harbor_clerk.api.schemas.documents import (
    CorpusOverviewResponse,
    DocumentContentResponse,
    DocumentDetail,
    DocumentEntitiesResponse,
    DocumentOutlineResponse,
    DocumentSummary,
    EntityOut,
    HeadingOut,
    JobInfo,
    PageContent,
    PaginatedDocuments,
    RelatedDocumentsResponse,
)
from harbor_clerk.api.scope import apply_key_scope
from harbor_clerk.audit import log_audit
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models import (
    Chunk,
    Document,
    DocumentHeading,
    DocumentPage,
    Entity,
    IngestionJob,
)
from harbor_clerk.models.enums import JobStage, PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFolder
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


@router.get("/docs", response_model=PaginatedDocuments)
async def list_documents(
    limit: int = Query(50, ge=0, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
    entity: str | None = Query(default=None, description="Filter by entity text (ILIKE)"),
    entity_type: str | None = Query(default=None, description="Filter by entity type"),
    mime_type: str | None = Query(default=None, description="Filter by MIME type"),
    language: str | None = Query(default=None, description="Filter by chunk language"),
    doc_type: str | None = Query(default=None, description="Filter by doc type"),
    doc_ids: str | None = Query(default=None, description="Comma-separated document IDs"),
    topic_id: int | None = Query(default=None, description="Filter by topic cluster ID"),
    sort: str = Query(default="updated", pattern="^(updated|created|title)$"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    base = select(Document).where(Document.status == "active")

    # Filter by explicit document IDs (e.g. from topic clusters)
    if doc_ids:
        id_list = []
        for d in doc_ids.split(","):
            d = d.strip()
            if d:
                try:
                    id_list.append(uuid.UUID(d))
                except ValueError:
                    pass
        if id_list:
            base = base.where(Document.doc_id.in_(id_list))

    if topic_id is not None:
        base = base.where(Document.topic_id == topic_id)

    if q:
        escaped = re.sub(r"([%_\\])", r"\\\1", q)
        pattern = f"%{escaped}%"
        base = base.where(Document.title.ilike(pattern) | Document.canonical_filename.ilike(pattern))

    # Flat filters directly on Document
    if mime_type:
        base = base.where(Document.mime_type == mime_type)
    if doc_type:
        base = base.where(Document.doc_type == doc_type)

    # Language filter: docs that have chunks in this language
    if language:
        lang_subq = select(Chunk.doc_id).where(Chunk.language == language).group_by(Chunk.doc_id).subquery()
        base = base.where(Document.doc_id.in_(select(lang_subq.c.doc_id)))

    # Entity filter: docs containing matching entities
    if entity:
        entity_filters = [Entity.entity_text.ilike(f"%{entity}%")]
        if entity_type:
            entity_filters.append(Entity.entity_type == entity_type)
        entity_subq = select(Entity.doc_id).where(*entity_filters).group_by(Entity.doc_id).subquery()
        base = base.where(Document.doc_id.in_(select(entity_subq.c.doc_id)))

    # Per-API-key scope filter (no-op for users / unrestricted keys)
    base = apply_key_scope(base, principal)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0

    # Sorting
    sort_column = {
        "updated": Document.updated_at,
        "created": Document.created_at,
        "title": Document.title,
    }[sort]
    order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()

    query = base.order_by(order).offset(offset)
    if limit > 0:
        query = query.limit(limit)

    result = await session.execute(query)
    docs = result.scalars().all()

    # Batch-fetch the latest summarize job status for every doc in this page.
    # DISTINCT ON keeps only the most recent row per doc.
    sum_job_status_by_doc: dict[str, str] = {}
    if docs:
        doc_ids_for_jobs = [d.doc_id for d in docs]
        rows = await session.execute(
            select(IngestionJob.doc_id, IngestionJob.status)
            .where(IngestionJob.doc_id.in_(doc_ids_for_jobs))
            .where(IngestionJob.stage == JobStage.summarize)
            .order_by(IngestionJob.doc_id, IngestionJob.created_at.desc())
            .distinct(IngestionJob.doc_id)
        )
        for did, jst in rows.all():
            sum_job_status_by_doc[str(did)] = jst.value if hasattr(jst, "value") else str(jst)

    summaries = []
    for doc in docs:
        summaries.append(
            DocumentSummary(
                doc_id=str(doc.doc_id),
                title=doc.title,
                canonical_filename=doc.canonical_filename,
                status=doc.status,
                pipeline_status=doc.pipeline_status.value if doc.pipeline_status else None,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                summary=doc.summary,
                summary_model=doc.summary_model,
                doc_type=doc.doc_type,
                source_path=doc.source_path,
                topic_id=doc.topic_id,
                summarize_job_status=sum_job_status_by_doc.get(str(doc.doc_id)),
            )
        )

    # Enrich with watched file info (batch lookup — graceful if table doesn't exist yet)
    if summaries:
        try:
            doc_ids_list = [uuid.UUID(s.doc_id) for s in summaries]
            wf_result = await session.execute(select(WatchedFile).where(WatchedFile.doc_id.in_(doc_ids_list)))
            wf_by_doc: dict[str, WatchedFile] = {}
            for wf in wf_result.scalars().all():
                if wf.doc_id:
                    wf_by_doc[str(wf.doc_id)] = wf
            for s in summaries:
                wf = wf_by_doc.get(s.doc_id)
                if wf:
                    folder_result = await session.execute(
                        select(WatchedFolder).where(WatchedFolder.folder_id == wf.folder_id)
                    )
                    folder = folder_result.scalar_one_or_none()
                    folder_path = folder.path if folder else ""
                    s.watch_source_path = f"{folder_path}/{wf.relative_path}" if folder_path else wf.relative_path
                    s.watch_status = wf.status.value
                    if folder is not None:
                        s.folder_name = folder.display_name or posixpath.basename(folder.path)
        except Exception:
            # watched_files table may not exist if migration 0011 hasn't run
            await session.rollback()

    return PaginatedDocuments(items=summaries, total=total, limit=limit, offset=offset)


# Must be defined before /docs/{doc_id} to avoid path capture
@router.get("/docs/overview", response_model=CorpusOverviewResponse)
async def corpus_overview(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Corpus-level statistics: counts, language distribution, mime types, date range, and document list."""
    # Per-API-key scope: limit aggregates to visible doc_ids for scoped keys.
    visible_ids: set[uuid.UUID] | None = None
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(
            select(Document.doc_id).where(Document.status == "active"),
            principal,
        )
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}

    doc_count_q = select(func.count()).select_from(Document).where(Document.status == "active")
    if visible_ids is not None:
        doc_count_q = doc_count_q.where(Document.doc_id.in_(visible_ids))
    doc_count = (await session.execute(doc_count_q)).scalar() or 0

    chunk_count_q = (
        select(func.count())
        .select_from(Chunk)
        .join(Document, Document.doc_id == Chunk.doc_id)
        .where(Document.status == "active")
    )
    if visible_ids is not None:
        chunk_count_q = chunk_count_q.where(Document.doc_id.in_(visible_ids))
    chunk_count = (await session.execute(chunk_count_q)).scalar() or 0

    total_pages_q = (
        select(func.count())
        .select_from(DocumentPage)
        .join(Document, Document.doc_id == DocumentPage.doc_id)
        .where(Document.status == "active")
    )
    if visible_ids is not None:
        total_pages_q = total_pages_q.where(Document.doc_id.in_(visible_ids))
    total_pages = (await session.execute(total_pages_q)).scalar() or 0

    lang_q = (
        select(Chunk.language, func.count())
        .join(Document, Document.doc_id == Chunk.doc_id)
        .where(Document.status == "active")
    )
    if visible_ids is not None:
        lang_q = lang_q.where(Document.doc_id.in_(visible_ids))
    lang_rows = (await session.execute(lang_q.group_by(Chunk.language).order_by(func.count().desc()))).all()
    languages = {row[0]: row[1] for row in lang_rows if row[0]}

    mime_q = select(Document.mime_type, func.count()).where(Document.status == "active", Document.mime_type.isnot(None))
    if visible_ids is not None:
        mime_q = mime_q.where(Document.doc_id.in_(visible_ids))
    mime_rows = (await session.execute(mime_q.group_by(Document.mime_type).order_by(func.count().desc()))).all()
    mime_types = {row[0]: row[1] for row in mime_rows if row[0]}

    date_q = select(func.min(Document.updated_at), func.max(Document.updated_at)).where(Document.status == "active")
    if visible_ids is not None:
        date_q = date_q.where(Document.doc_id.in_(visible_ids))
    date_row = (await session.execute(date_q)).one()

    docs_q = select(Document).where(Document.status == "active")
    if visible_ids is not None:
        docs_q = docs_q.where(Document.doc_id.in_(visible_ids))
    result = await session.execute(docs_q.order_by(Document.updated_at.desc()).limit(200))
    docs = result.scalars().all()

    items = []
    for doc in docs:
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": doc.summary,
                "status": doc.pipeline_status.value if doc.pipeline_status else None,
                "updated_at": doc.updated_at,
            }
        )

    return CorpusOverviewResponse(
        document_count=doc_count,
        total_chunks=chunk_count,
        total_pages=total_pages,
        languages=languages,
        mime_types=mime_types,
        date_range={
            "oldest": date_row[0],
            "newest": date_row[1],
        },
        documents=items,
        truncated=doc_count > len(items),
    )


@router.get("/docs/entities/autocomplete")
async def entity_autocomplete(
    q: str = Query(min_length=1, max_length=100),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Fast entity autocomplete with ILIKE matching across all active documents."""
    if len(q) < 2:
        return []

    filters = [
        Document.status == "active",
        Entity.entity_text.ilike(f"%{q}%"),
    ]
    if entity_type:
        filters.append(Entity.entity_type == entity_type)

    query = (
        select(
            Entity.entity_text,
            Entity.entity_type,
            func.count(func.distinct(Entity.doc_id)).label("doc_count"),
        )
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(*filters)
    )
    # Per-API-key scope: filter to visible docs only (no-op for users / unrestricted keys).
    query = apply_key_scope(query, principal)
    result = await session.execute(
        query.group_by(Entity.entity_text, Entity.entity_type)
        .order_by(func.count(func.distinct(Entity.doc_id)).desc())
        .limit(limit)
    )
    rows = result.all()
    return [{"entity_text": r.entity_text, "entity_type": r.entity_type, "doc_count": r.doc_count} for r in rows]


@router.get("/docs/entities/top")
async def top_entities(
    entity_type: str = Query(...),
    limit: int = Query(default=30, ge=1, le=100),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Top entities of a given type across active documents."""
    query = (
        select(
            Entity.entity_text,
            func.count(func.distinct(Entity.doc_id)).label("doc_count"),
        )
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(Document.status == "active", Entity.entity_type == entity_type)
    )
    # Per-API-key scope: filter to visible docs only (no-op for users / unrestricted keys).
    query = apply_key_scope(query, principal)
    result = await session.execute(
        query.group_by(Entity.entity_text).order_by(func.count(func.distinct(Entity.doc_id)).desc()).limit(limit)
    )
    return [{"entity_text": r[0], "doc_count": r[1]} for r in result.all()]


@router.get("/docs/filters")
async def document_filters(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return available filter values for the documents list."""
    # Per-API-key scope: limit facet counts to visible docs for scoped keys.
    visible_ids: set[uuid.UUID] | None = None
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(
            select(Document.doc_id).where(Document.status == "active"),
            principal,
        )
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}

    # MIME types
    mime_q = select(Document.mime_type, func.count()).where(Document.status == "active", Document.mime_type.isnot(None))
    if visible_ids is not None:
        mime_q = mime_q.where(Document.doc_id.in_(visible_ids))
    mime_rows = (await session.execute(mime_q.group_by(Document.mime_type).order_by(func.count().desc()))).all()

    # Doc types
    doc_type_q = select(Document.doc_type, func.count()).where(
        Document.status == "active", Document.doc_type.isnot(None)
    )
    if visible_ids is not None:
        doc_type_q = doc_type_q.where(Document.doc_id.in_(visible_ids))
    doc_type_rows = (await session.execute(doc_type_q.group_by(Document.doc_type).order_by(func.count().desc()))).all()

    # Languages
    lang_q = (
        select(Chunk.language, func.count(func.distinct(Chunk.doc_id)))
        .join(Document, Document.doc_id == Chunk.doc_id)
        .where(Document.status == "active", Chunk.language.isnot(None))
    )
    if visible_ids is not None:
        lang_q = lang_q.where(Document.doc_id.in_(visible_ids))
    lang_rows = (
        await session.execute(lang_q.group_by(Chunk.language).order_by(func.count(func.distinct(Chunk.doc_id)).desc()))
    ).all()

    # Entity types
    ent_type_q = (
        select(Entity.entity_type, func.count(func.distinct(Entity.doc_id)))
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(Document.status == "active")
    )
    if visible_ids is not None:
        ent_type_q = ent_type_q.where(Document.doc_id.in_(visible_ids))
    ent_type_rows = (
        await session.execute(
            ent_type_q.group_by(Entity.entity_type).order_by(func.count(func.distinct(Entity.doc_id)).desc())
        )
    ).all()

    return {
        "mime_types": [{"value": r[0], "count": r[1]} for r in mime_rows],
        "doc_types": [{"value": r[0], "count": r[1]} for r in doc_type_rows],
        "languages": [{"value": r[0], "count": r[1]} for r in lang_rows],
        "entity_types": [{"value": r[0], "count": r[1]} for r in ent_type_rows],
    }


@router.get("/docs/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    doc_query = apply_key_scope(
        select(Document).where(Document.doc_id == doc_id),
        principal,
    )
    result = await session.execute(doc_query)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Load jobs for this doc
    jobs_result = await session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id))
    job_rows = jobs_result.scalars().all()

    jobs = []
    for j in job_rows:
        job_status = j.status.value
        error = j.error
        # Surface skipped stages with reason instead of showing "done"
        if j.metrics and j.metrics.get("skipped"):
            job_status = "skipped"
            reason = j.metrics.get("reason", "")
            if reason == "spacy_unavailable":
                error = "spaCy NER models not available"
            elif reason:
                error = reason
        jobs.append(
            JobInfo(
                job_id=str(j.job_id),
                stage=j.stage.value,
                status=job_status,
                progress_current=j.progress_current,
                progress_total=j.progress_total,
                error=error,
                created_at=j.created_at,
                started_at=j.started_at,
                finished_at=j.finished_at,
            )
        )

    return DocumentDetail(
        doc_id=str(doc.doc_id),
        title=doc.title,
        canonical_filename=doc.canonical_filename,
        status=doc.status,
        pipeline_status=doc.pipeline_status.value if doc.pipeline_status else None,
        pipeline_seq=doc.pipeline_seq,
        summary=doc.summary,
        doc_type=doc.doc_type,
        mime_type=doc.mime_type,
        source_path=doc.source_path,
        has_text_layer=doc.has_text_layer,
        needs_ocr=doc.needs_ocr,
        extracted_chars=doc.extracted_chars,
        size_bytes=doc.size_bytes,
        ocr_languages_used=doc.ocr_languages_used,
        error=doc.error,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        jobs=jobs,
    )


@router.get("/docs/{doc_id}/content", response_model=DocumentContentResponse)
async def get_document_content(
    doc_id: uuid.UUID,
    pages: str | None = Query(default=None, description="Page range e.g. '1-3'"),
    max_chars: int | None = Query(default=None),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    # Get document
    doc_query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
    doc_query = apply_key_scope(doc_query, principal)
    result = await session.execute(doc_query)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Build page query — directly by doc_id
    query = select(DocumentPage).where(DocumentPage.doc_id == doc_id).order_by(DocumentPage.page_num)

    if pages is not None:
        # Parse "1-3" or "5"
        parts = pages.split("-")
        try:
            if len(parts) == 2:
                start, end = int(parts[0]), int(parts[1])
                query = query.where(
                    DocumentPage.page_num >= start,
                    DocumentPage.page_num <= end,
                )
            elif len(parts) == 1:
                query = query.where(DocumentPage.page_num == int(parts[0]))
            else:
                raise ValueError("invalid format")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid page range: '{pages}'. Use a number or range like '1-3'.",
            )

    page_result = await session.execute(query)
    page_rows = page_result.scalars().all()

    page_contents = []
    total_chars = 0
    for p in page_rows:
        text = p.page_text
        if max_chars is not None and total_chars + len(text) > max_chars:
            text = text[: max_chars - total_chars]
            page_contents.append(
                PageContent(
                    page_num=p.page_num,
                    text=text,
                    ocr_used=p.ocr_used,
                    ocr_confidence=p.ocr_confidence,
                )
            )
            total_chars += len(text)
            break
        page_contents.append(
            PageContent(
                page_num=p.page_num,
                text=text,
                ocr_used=p.ocr_used,
                ocr_confidence=p.ocr_confidence,
            )
        )
        total_chars += len(text)

    return DocumentContentResponse(
        doc_id=str(doc_id),
        pages=page_contents,
        total_chars=total_chars,
    )


@router.get("/docs/{doc_id}/outline", response_model=DocumentOutlineResponse)
async def get_document_outline(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Get document heading outline/structure with page and chunk counts."""
    doc_query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
    doc_query = apply_key_scope(doc_query, principal)
    result = await session.execute(doc_query)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Fetch headings, page count, chunk count — all keyed by doc_id
    headings_result = await session.execute(
        select(DocumentHeading).where(DocumentHeading.doc_id == doc_id).order_by(DocumentHeading.position)
    )
    headings = headings_result.scalars().all()

    page_count_result = await session.execute(
        select(func.count()).select_from(DocumentPage).where(DocumentPage.doc_id == doc_id)
    )
    page_count = page_count_result.scalar_one()

    chunk_count_result = await session.execute(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id))
    chunk_count = chunk_count_result.scalar_one()

    return DocumentOutlineResponse(
        doc_id=str(doc_id),
        title=doc.title,
        page_count=page_count,
        chunk_count=chunk_count,
        headings=[HeadingOut(level=h.level, title=h.title, page_num=h.page_num) for h in headings],
    )


@router.get("/docs/{doc_id}/entities", response_model=DocumentEntitiesResponse)
async def get_document_entities(
    doc_id: uuid.UUID,
    entity_type: str | None = Query(default=None, description="Filter by entity type"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Get deduplicated entities with mention counts for a document."""
    doc_query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
    doc_query = apply_key_scope(doc_query, principal)
    result = await session.execute(doc_query)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    filters = [Entity.doc_id == doc_id]
    if entity_type:
        filters.append(Entity.entity_type == entity_type)

    # Deduplicated entities with mention counts
    count_q = (
        select(
            Entity.entity_text,
            Entity.entity_type,
            func.count().label("mention_count"),
        )
        .where(*filters)
        .group_by(Entity.entity_text, Entity.entity_type)
    )

    total = (await session.execute(select(func.count()).select_from(count_q.subquery()))).scalar() or 0

    rows = (await session.execute(count_q.order_by(func.count().desc()).offset(offset).limit(limit))).all()

    entities = [EntityOut(entity_text=r[0], entity_type=r[1], mention_count=r[2]) for r in rows]

    # Get all distinct entity types for this doc
    type_rows = (
        (
            await session.execute(
                select(Entity.entity_type).where(Entity.doc_id == doc_id).distinct().order_by(Entity.entity_type)
            )
        )
        .scalars()
        .all()
    )

    return DocumentEntitiesResponse(
        doc_id=str(doc_id),
        entities=entities,
        total=total,
        entity_types=list(type_rows),
    )


@router.get("/docs/{doc_id}/related", response_model=RelatedDocumentsResponse)
async def find_related_documents(
    doc_id: uuid.UUID,
    k: int = Query(default=5, ge=1, le=20),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Find documents most similar to the given document by embedding cosine similarity."""
    doc_query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
    doc_query = apply_key_scope(doc_query, principal)
    doc = (await session.execute(doc_query)).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Get embeddings for this document's chunks (all keyed by doc_id)
    rows = (
        await session.execute(
            select(Chunk.embedding).where(
                Chunk.doc_id == doc_id,
                Chunk.embedding.isnot(None),
            )
        )
    ).all()

    if not rows:
        return RelatedDocumentsResponse(doc_id=str(doc_id), related=[])

    # Average embeddings
    dim = len(rows[0][0])
    avg = [0.0] * dim
    for (emb,) in rows:
        for i, v in enumerate(emb):
            avg[i] += v
    n = len(rows)
    avg = [v / n for v in avg]

    # Find nearest docs — scope-filter the candidate set so scoped keys
    # only see related docs within their allowed topics/folders.
    distance = Chunk.embedding.cosine_distance(avg)
    nearest_q = (
        select(Chunk.doc_id, func.min(distance).label("min_distance"))
        .join(Document, Document.doc_id == Chunk.doc_id)
        .where(
            Document.status == "active",
            Chunk.embedding.isnot(None),
            Chunk.doc_id != doc_id,
        )
    )
    nearest_q = apply_key_scope(nearest_q, principal)
    nearest = (await session.execute(nearest_q.group_by(Chunk.doc_id).order_by(func.min(distance)).limit(k))).all()

    if not nearest:
        return RelatedDocumentsResponse(doc_id=str(doc_id), related=[])

    related_ids = [row[0] for row in nearest]
    distances = {row[0]: float(row[1]) for row in nearest}

    docs_result = await session.execute(select(Document).where(Document.doc_id.in_(related_ids)))
    related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in related_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": rdoc.summary,
                "similarity": round(1.0 - distances[rid], 4),
                "doc_type": rdoc.doc_type,
                "mime_type": rdoc.mime_type,
                "canonical_filename": rdoc.canonical_filename,
            }
        )

    return RelatedDocumentsResponse(doc_id=str(doc_id), related=items)


@router.get("/docs/{doc_id}/download")
async def download_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Download the original file for a document.

    Gated by the ``allow_source_download`` setting (env: ALLOW_SOURCE_DOWNLOAD).
    Disabled by default everywhere because the response exposes the raw bytes
    of the original document — meaningfully more data than the chunk excerpts
    that read-only API keys were designed to surface. On macOS native, the menu
    app intentionally does NOT expose a way to enable this; use Reveal in
    Finder instead. On Docker, an admin can opt in by setting the env var.
    """
    if not get_settings().allow_source_download:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Source-file download is disabled on this deployment. "
                "On macOS, use Reveal in Finder. "
                "On Docker, an admin can enable downloads by setting ALLOW_SOURCE_DOWNLOAD=true."
            ),
        )

    query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
    query = apply_key_scope(query, principal)
    result = await session.execute(query)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    import os
    from pathlib import Path

    # Read from storage if available; fall back to source_path for watched folder files
    if doc.original_object_key:
        storage = get_storage()
        obj = storage.get_object(doc.original_bucket, doc.original_object_key)
        file_bytes = obj.read()
        filename = posixpath.basename(doc.original_object_key)
    elif doc.source_path and os.path.exists(doc.source_path):
        # Watched folder file — no stored object, read from original location
        file_bytes = Path(doc.source_path).read_bytes()
        filename = posixpath.basename(doc.source_path)
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source file unavailable")

    content_type = doc.mime_type or "application/octet-stream"

    from urllib.parse import quote

    # RFC 8187 encoding for non-ASCII filenames (French accents, etc.)
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"

    return Response(
        content=file_bytes,
        media_type=content_type,
        headers={
            "Content-Disposition": disposition,
        },
    )


@router.delete("/docs/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Document).where(Document.doc_id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    doc.status = "deleted"
    await log_audit(
        session,
        user_id=admin.id,
        action="delete_document",
        target_type="document",
        target_id=doc_id,
    )
    await session.commit()


@router.post("/docs/{doc_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess_document(
    doc_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Document).where(Document.doc_id == doc_id, Document.status == "active"))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Bump pipeline_seq to invalidate any in-flight workers
    doc.pipeline_seq = (doc.pipeline_seq or 0) + 1
    doc.pipeline_status = PipelineStatus.queued
    doc.error = None

    await log_audit(
        session,
        user_id=admin.id,
        action="reprocess_document",
        target_type="document",
        target_id=doc_id,
    )
    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage, reset_jobs

    reset_jobs(doc_id)
    enqueue_stage(doc_id, JobStage.extract)

    return {
        "doc_id": str(doc_id),
        "status": "reprocessing",
    }


@router.post("/docs/{doc_id}/resummarize", status_code=status.HTTP_202_ACCEPTED)
async def resummarize_document(
    doc_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Re-run only the summarize stage on a document."""
    result = await session.execute(select(Document).where(Document.doc_id == doc_id, Document.status == "active"))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage

    enqueue_stage(doc_id, JobStage.summarize)

    return {
        "doc_id": str(doc_id),
        "status": "resummarizing",
    }


@router.post("/docs/{doc_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_processing(
    doc_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Document).where(Document.doc_id == doc_id, Document.status == "active"))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    from harbor_clerk.worker.pipeline import cancel_doc_jobs

    total_cancelled = cancel_doc_jobs(doc_id)

    await log_audit(
        session,
        user_id=admin.id,
        action="cancel_processing",
        target_type="document",
        target_id=doc_id,
        detail={"cancelled_jobs": total_cancelled},
    )
    await session.commit()

    return {"doc_id": str(doc_id), "cancelled_jobs": total_cancelled}
