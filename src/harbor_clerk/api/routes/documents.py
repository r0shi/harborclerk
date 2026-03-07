"""Document CRUD endpoints."""

import logging
import posixpath
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    VersionInfo,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.db import get_session
from harbor_clerk.models import (
    Chunk,
    Document,
    DocumentHeading,
    DocumentPage,
    DocumentVersion,
    Entity,
    IngestionJob,
)
from harbor_clerk.models.enums import JobStage, VersionStatus
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


@router.get("/docs", response_model=PaginatedDocuments)
async def list_documents(
    limit: int = Query(50, ge=0, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    base = select(Document).where(Document.status == "active")
    if q:
        escaped = re.sub(r"([%_\\])", r"\\\1", q)
        pattern = f"%{escaped}%"
        base = base.where(Document.title.ilike(pattern) | Document.canonical_filename.ilike(pattern))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0

    query = base.options(selectinload(Document.versions)).order_by(Document.updated_at.desc()).offset(offset)
    if limit > 0:
        query = query.limit(limit)

    result = await session.execute(query)
    docs = result.scalars().all()

    summaries = []
    for doc in docs:
        latest_status = None
        latest_summary = None
        latest_summary_model = None
        latest_doc_type = None
        latest_source_path = None
        version_count = len(doc.versions) if doc.versions else 0
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    latest_status = v.status.value
                    latest_summary = v.summary
                    latest_summary_model = v.summary_model
                    latest_doc_type = v.doc_type
                    latest_source_path = v.source_path
                    break
        if latest_status is None and doc.versions:
            latest_v = doc.versions[-1]
            latest_status = latest_v.status.value
            latest_summary = latest_v.summary
            latest_summary_model = latest_v.summary_model
            latest_doc_type = latest_v.doc_type
            latest_source_path = latest_v.source_path

        summaries.append(
            DocumentSummary(
                doc_id=str(doc.doc_id),
                title=doc.title,
                canonical_filename=doc.canonical_filename,
                status=doc.status,
                latest_version_status=latest_status,
                version_count=version_count,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                summary=latest_summary,
                summary_model=latest_summary_model,
                doc_type=latest_doc_type,
                source_path=latest_source_path,
            )
        )
    return PaginatedDocuments(items=summaries, total=total, limit=limit, offset=offset)


# Must be defined before /docs/{doc_id} to avoid path capture
@router.get("/docs/overview", response_model=CorpusOverviewResponse)
async def corpus_overview(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Corpus-level statistics: counts, language distribution, mime types, date range, and document list."""
    doc_count = (
        await session.execute(select(func.count()).select_from(Document).where(Document.status == "active"))
    ).scalar() or 0

    chunk_count = (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.latest_version_id == Chunk.version_id)
            .where(Document.status == "active")
        )
    ).scalar() or 0

    total_pages = (
        await session.execute(
            select(func.count())
            .select_from(DocumentPage)
            .join(Document, Document.latest_version_id == DocumentPage.version_id)
            .where(Document.status == "active")
        )
    ).scalar() or 0

    lang_rows = (
        await session.execute(
            select(Chunk.language, func.count())
            .join(Document, Document.latest_version_id == Chunk.version_id)
            .where(Document.status == "active")
            .group_by(Chunk.language)
            .order_by(func.count().desc())
        )
    ).all()
    languages = {row[0]: row[1] for row in lang_rows if row[0]}

    mime_rows = (
        await session.execute(
            select(DocumentVersion.mime_type, func.count())
            .join(Document, Document.latest_version_id == DocumentVersion.version_id)
            .where(Document.status == "active")
            .group_by(DocumentVersion.mime_type)
            .order_by(func.count().desc())
        )
    ).all()
    mime_types = {row[0]: row[1] for row in mime_rows if row[0]}

    date_row = (
        await session.execute(
            select(func.min(Document.updated_at), func.max(Document.updated_at)).where(Document.status == "active")
        )
    ).one()

    result = await session.execute(
        select(Document)
        .options(selectinload(Document.versions))
        .where(Document.status == "active")
        .order_by(Document.updated_at.desc())
        .limit(200)
    )
    docs = result.scalars().all()

    items = []
    for doc in docs:
        summary = None
        ver_status = None
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    summary = v.summary
                    ver_status = v.status.value
                    break
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": summary,
                "status": ver_status,
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


@router.get("/docs/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Document).where(Document.doc_id == doc_id).options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Load jobs for each version
    version_ids = [v.version_id for v in (doc.versions or [])]
    jobs_by_version: dict[uuid.UUID, list[IngestionJob]] = {}
    if version_ids:
        jobs_result = await session.execute(select(IngestionJob).where(IngestionJob.version_id.in_(version_ids)))
        for job in jobs_result.scalars().all():
            jobs_by_version.setdefault(job.version_id, []).append(job)

    versions = []
    for v in doc.versions or []:
        jobs = [
            JobInfo(
                job_id=str(j.job_id),
                stage=j.stage.value,
                status=j.status.value,
                progress_current=j.progress_current,
                progress_total=j.progress_total,
                error=j.error,
                created_at=j.created_at,
                started_at=j.started_at,
                finished_at=j.finished_at,
            )
            for j in jobs_by_version.get(v.version_id, [])
        ]
        versions.append(
            VersionInfo(
                version_id=str(v.version_id),
                status=v.status.value,
                mime_type=v.mime_type,
                size_bytes=v.size_bytes,
                has_text_layer=v.has_text_layer,
                needs_ocr=v.needs_ocr,
                extracted_chars=v.extracted_chars,
                source_path=v.source_path,
                error=v.error,
                created_at=v.created_at,
                jobs=jobs,
            )
        )

    return DocumentDetail(
        doc_id=str(doc.doc_id),
        title=doc.title,
        canonical_filename=doc.canonical_filename,
        status=doc.status,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        versions=versions,
    )


@router.get("/docs/{doc_id}/content", response_model=DocumentContentResponse)
async def get_document_content(
    doc_id: uuid.UUID,
    pages: str | None = Query(default=None, description="Page range e.g. '1-3'"),
    max_chars: int | None = Query(default=None),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    # Get document + latest version
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No versions available")

    # Build page query
    query = select(DocumentPage).where(DocumentPage.version_id == version_id).order_by(DocumentPage.page_num)

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
        version_id=str(version_id),
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
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No versions available")

    # Fetch headings, page count, chunk count
    headings_result = await session.execute(
        select(DocumentHeading).where(DocumentHeading.version_id == version_id).order_by(DocumentHeading.position)
    )
    headings = headings_result.scalars().all()

    page_count_result = await session.execute(
        select(func.count()).select_from(DocumentPage).where(DocumentPage.version_id == version_id)
    )
    page_count = page_count_result.scalar_one()

    chunk_count_result = await session.execute(
        select(func.count()).select_from(Chunk).where(Chunk.version_id == version_id)
    )
    chunk_count = chunk_count_result.scalar_one()

    return DocumentOutlineResponse(
        doc_id=str(doc_id),
        version_id=str(version_id),
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
    """Get deduplicated entities with mention counts for a document's latest version."""
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        return DocumentEntitiesResponse(doc_id=str(doc_id), entities=[], total=0, entity_types=[])

    filters = [Entity.version_id == version_id]
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

    # Get all distinct entity types for this version
    type_rows = (
        (
            await session.execute(
                select(Entity.entity_type)
                .where(Entity.version_id == version_id)
                .distinct()
                .order_by(Entity.entity_type)
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
    doc = (
        await session.execute(select(Document).where(Document.doc_id == doc_id, Document.status == "active"))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No versions available")

    # Get embeddings for this document
    rows = (
        await session.execute(
            select(Chunk.embedding).where(
                Chunk.version_id == version_id,
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

    # Find nearest docs
    distance = Chunk.embedding.cosine_distance(avg)
    nearest = (
        await session.execute(
            select(Chunk.doc_id, func.min(distance).label("min_distance"))
            .join(Document, Document.latest_version_id == Chunk.version_id)
            .where(
                Document.status == "active",
                Chunk.embedding.isnot(None),
                Chunk.doc_id != doc_id,
            )
            .group_by(Chunk.doc_id)
            .order_by(func.min(distance))
            .limit(k)
        )
    ).all()

    if not nearest:
        return RelatedDocumentsResponse(doc_id=str(doc_id), related=[])

    related_ids = [row[0] for row in nearest]
    distances = {row[0]: float(row[1]) for row in nearest}

    docs_result = await session.execute(
        select(Document).options(selectinload(Document.versions)).where(Document.doc_id.in_(related_ids))
    )
    related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in related_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        summary = None
        if rdoc.latest_version_id and rdoc.versions:
            for v in rdoc.versions:
                if v.version_id == rdoc.latest_version_id:
                    summary = v.summary
                    break
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": summary,
                "similarity": round(1.0 - distances[rid], 4),
            }
        )

    return RelatedDocumentsResponse(doc_id=str(doc_id), related=items)


@router.get("/docs/{doc_id}/download")
async def download_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Download the original file for the latest version of a document."""
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No versions available")

    ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id))
    version = ver_result.scalar_one()

    storage = get_storage()
    obj = storage.get_object(version.original_bucket, version.original_object_key)
    filename = posixpath.basename(version.original_object_key)
    content_type = version.mime_type or "application/octet-stream"

    from urllib.parse import quote

    # RFC 8187 encoding for non-ASCII filenames (French accents, etc.)
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"

    return Response(
        content=obj.read(),
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
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No version to reprocess")

    # Reset version status
    ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id))
    version = ver_result.scalar_one()
    version.status = VersionStatus.queued
    version.error = None

    await log_audit(
        session,
        user_id=admin.id,
        action="reprocess_document",
        target_type="document",
        target_id=doc_id,
        detail={"version_id": str(version_id)},
    )
    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage, reset_jobs

    reset_jobs(version_id)
    enqueue_stage(version_id, JobStage.extract)

    return {
        "doc_id": str(doc_id),
        "version_id": str(version_id),
        "status": "reprocessing",
    }


@router.post("/docs/{doc_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_processing(
    doc_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    from harbor_clerk.worker.pipeline import cancel_version_jobs

    total_cancelled = 0
    for v in doc.versions or []:
        total_cancelled += cancel_version_jobs(v.version_id)

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
