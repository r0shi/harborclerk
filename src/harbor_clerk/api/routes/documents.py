"""Document CRUD endpoints."""

import logging
import posixpath
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from harbor_clerk.api.deps import Principal, require_admin, require_read_access
from harbor_clerk.api.schemas.documents import (
    DocumentContentResponse,
    DocumentDetail,
    DocumentSummary,
    JobInfo,
    PageContent,
    VersionInfo,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.db import get_session
from harbor_clerk.models import Document, DocumentPage, DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage, VersionStatus
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


@router.get("/docs", response_model=list[DocumentSummary])
async def list_documents(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Document)
        .where(Document.status == "active")
        .options(selectinload(Document.versions))
        .order_by(Document.updated_at.desc())
        .limit(200)
    )
    docs = result.scalars().all()

    summaries = []
    for doc in docs:
        latest_status = None
        version_count = len(doc.versions) if doc.versions else 0
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    latest_status = v.status.value
                    break
        if latest_status is None and doc.versions:
            latest_status = doc.versions[-1].status.value

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
            )
        )
    return summaries


@router.get("/docs/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id)
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    # Load jobs for each version
    version_ids = [v.version_id for v in (doc.versions or [])]
    jobs_by_version: dict[uuid.UUID, list[IngestionJob]] = {}
    if version_ids:
        jobs_result = await session.execute(
            select(IngestionJob).where(IngestionJob.version_id.in_(version_ids))
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No versions available"
        )

    # Build page query
    query = (
        select(DocumentPage)
        .where(DocumentPage.version_id == version_id)
        .order_by(DocumentPage.page_num)
    )

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No versions available"
        )

    ver_result = await session.execute(
        select(DocumentVersion).where(DocumentVersion.version_id == version_id)
    )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No version to reprocess"
        )

    # Reset version status
    ver_result = await session.execute(
        select(DocumentVersion).where(DocumentVersion.version_id == version_id)
    )
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

    from harbor_clerk.worker.pipeline import enqueue_stage

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

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
