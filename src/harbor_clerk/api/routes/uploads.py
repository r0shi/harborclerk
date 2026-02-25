"""Upload endpoints: upload files, confirm, list status."""

import hashlib
import io
import logging
import mimetypes
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_user
from harbor_clerk.api.schemas.uploads import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchConfirmResultItem,
    ConfirmUploadRequest,
    ConfirmUploadResponse,
    UploadFileResult,
    UploadResponse,
    UploadStatusResponse,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.config import Settings, get_settings
from harbor_clerk.db import get_session
from harbor_clerk.storage import get_storage
from harbor_clerk.models import Document, DocumentVersion, Upload
from harbor_clerk.models.enums import JobStage, VersionStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])

ALLOWED_EXTENSIONS = {
    # Documents
    ".pdf", ".docx", ".doc", ".rtf", ".txt", ".md",
    ".odt", ".pages",
    # Spreadsheets
    ".xlsx", ".xls", ".ods", ".numbers", ".csv",
    # Presentations
    ".pptx", ".ppt", ".odp", ".key",
    # Images (OCR)
    ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    # eBooks
    ".epub",
    # Web
    ".html", ".htm",
    # Email
    ".eml",
}


@router.post("/uploads", response_model=UploadResponse)
async def upload_files(
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    max_file_bytes = settings.max_file_size_mb * 1024 * 1024
    max_batch_bytes = settings.max_batch_size_mb * 1024 * 1024
    results: list[UploadFileResult] = []
    batch_total = 0

    for file in files:
        # Skip files with unsupported extensions
        fname = file.filename or ""
        dot = fname.rfind(".")
        ext = fname[dot:].lower() if dot != -1 else ""
        if ext not in ALLOWED_EXTENSIONS:
            results.append(UploadFileResult(
                upload_id=str(uuid.uuid4()),
                filename=fname or "unknown",
                size_bytes=0,
                mime_type=file.content_type or "application/octet-stream",
                status="skipped",
            ))
            continue

        # Stream file, compute SHA256, buffer content
        sha = hashlib.sha256()
        chunks: list[bytes] = []
        total_size = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            chunks.append(chunk)
            total_size += len(chunk)
            if total_size > max_file_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File '{file.filename}' exceeds {settings.max_file_size_mb}MB limit",
                )

        batch_total += total_size
        if batch_total > max_batch_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Batch exceeds {settings.max_batch_size_mb}MB limit",
            )

        sha256_bytes = sha.digest()
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"

        # Check for duplicate by SHA256
        dup_result = await session.execute(
            select(DocumentVersion).where(DocumentVersion.original_sha256 == sha256_bytes)
        )
        dup_version = dup_result.scalar_one_or_none()

        if dup_version is not None:
            upload_row = Upload(
                user_id=principal.id if principal.type == "user" else None,
                original_filename=file.filename or "unknown",
                mime_type=mime,
                size_bytes=total_size,
                sha256=sha256_bytes,
                minio_bucket=settings.minio_bucket,
                minio_object_key="",  # not stored for duplicates
                doc_id=dup_version.doc_id,
                version_id=dup_version.version_id,
                status="duplicate",
            )
            session.add(upload_row)
            await session.flush()
            results.append(UploadFileResult(
                upload_id=str(upload_row.upload_id),
                filename=file.filename or "unknown",
                size_bytes=total_size,
                mime_type=mime,
                status="duplicate",
                duplicate_doc_id=str(dup_version.doc_id),
                duplicate_version_id=str(dup_version.version_id),
            ))
            continue

        # Store at temp key — sanitize filename to prevent path traversal
        safe_name = os.path.basename(file.filename or "file") or "file"
        temp_key = f"tmp/uploads/{uuid.uuid4()}/{safe_name}"
        content = b"".join(chunks)
        storage = get_storage()
        storage.put_object(
            settings.minio_bucket,
            temp_key,
            io.BytesIO(content),
            length=total_size,
            content_type=mime,
        )

        upload_row = Upload(
            user_id=principal.id if principal.type == "user" else None,
            original_filename=file.filename or "unknown",
            mime_type=mime,
            size_bytes=total_size,
            sha256=sha256_bytes,
            minio_bucket=settings.minio_bucket,
            minio_object_key=temp_key,
            status="pending_confirmation",
        )
        session.add(upload_row)
        await session.flush()

        results.append(UploadFileResult(
            upload_id=str(upload_row.upload_id),
            filename=file.filename or "unknown",
            size_bytes=total_size,
            mime_type=mime,
            status="pending_confirmation",
        ))

    await session.commit()
    return UploadResponse(files=results)


async def _confirm_single(
    session: AsyncSession,
    upload_id_str: str,
    action: str,
    existing_doc_id: str | None,
    principal: Principal,
    settings: Settings,
) -> ConfirmUploadResponse:
    """Core confirm logic shared by single and batch endpoints.

    Raises HTTPException on validation errors.
    """
    try:
        upload_uuid = uuid.UUID(upload_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid upload_id: not a valid UUID",
        )
    result = await session.execute(
        select(Upload).where(Upload.upload_id == upload_uuid)
    )
    upload = result.scalar_one_or_none()
    if upload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    if upload.status != "pending_confirmation":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Upload status is '{upload.status}', not pending_confirmation")

    if action == "new_document":
        filename = upload.original_filename
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        doc = Document(title=title, canonical_filename=filename)
        session.add(doc)
        await session.flush()
        doc_id = doc.doc_id

    elif action == "new_version":
        if existing_doc_id is None:
            raise HTTPException(status_code=422, detail="existing_doc_id required for new_version")
        try:
            doc_id = uuid.UUID(existing_doc_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid existing_doc_id: not a valid UUID",
            )
        doc_result = await session.execute(
            select(Document).where(Document.doc_id == doc_id, Document.status == "active")
        )
        doc = doc_result.scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    else:
        raise HTTPException(status_code=422, detail="action must be 'new_document' or 'new_version'")

    version = DocumentVersion(
        doc_id=doc_id,
        original_sha256=upload.sha256,
        original_bucket=settings.minio_bucket,
        original_object_key="",
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        status=VersionStatus.queued,
    )
    session.add(version)
    await session.flush()

    safe_name = os.path.basename(upload.original_filename) or "file"
    canonical_key = f"versions/{version.version_id}/{safe_name}"
    storage = get_storage()
    storage.copy_and_delete(
        upload.minio_bucket, upload.minio_object_key,
        settings.minio_bucket, canonical_key,
    )
    version.original_object_key = canonical_key

    upload.doc_id = doc_id
    upload.version_id = version.version_id
    upload.status = "processing"

    await log_audit(
        session,
        user_id=principal.id if principal.type == "user" else None,
        action="confirm_upload",
        target_type="document_version",
        target_id=version.version_id,
        detail={"action": action, "doc_id": str(doc_id)},
    )

    return ConfirmUploadResponse(
        doc_id=str(doc_id),
        version_id=str(version.version_id),
        status="processing",
    )


@router.post("/uploads/confirm", response_model=ConfirmUploadResponse)
async def confirm_upload(
    body: ConfirmUploadRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    result = await _confirm_single(
        session, body.upload_id, body.action, body.existing_doc_id,
        principal, settings,
    )
    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage
    enqueue_stage(uuid.UUID(result.version_id), JobStage.extract)

    return result


@router.post("/uploads/confirm-batch", response_model=BatchConfirmResponse)
async def confirm_upload_batch(
    body: BatchConfirmRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    results: list[BatchConfirmResultItem] = []
    version_ids_to_enqueue: list[uuid.UUID] = []

    for item in body.items:
        try:
            confirm_result = await _confirm_single(
                session, item.upload_id, item.action, item.existing_doc_id,
                principal, settings,
            )
            results.append(BatchConfirmResultItem(
                upload_id=item.upload_id,
                doc_id=confirm_result.doc_id,
                version_id=confirm_result.version_id,
                status=confirm_result.status,
            ))
            version_ids_to_enqueue.append(uuid.UUID(confirm_result.version_id))
        except HTTPException as exc:
            results.append(BatchConfirmResultItem(
                upload_id=item.upload_id,
                status="error",
                error=exc.detail,
            ))

    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage
    for vid in version_ids_to_enqueue:
        enqueue_stage(vid, JobStage.extract)

    return BatchConfirmResponse(results=results)


@router.get("/uploads", response_model=list[UploadStatusResponse])
async def list_uploads(
    since: datetime | None = Query(default=None),
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(Upload).order_by(Upload.created_at.desc()).limit(100)
    if since is not None:
        query = query.where(Upload.created_at >= since)

    result = await session.execute(query)
    uploads = result.scalars().all()
    return [
        UploadStatusResponse(
            upload_id=str(u.upload_id),
            original_filename=u.original_filename,
            status=u.status,
            doc_id=str(u.doc_id) if u.doc_id else None,
            version_id=str(u.version_id) if u.version_id else None,
            created_at=u.created_at,
        )
        for u in uploads
    ]
