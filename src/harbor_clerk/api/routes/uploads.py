"""Upload endpoints: upload files, confirm, list status."""

import hashlib
import io
import logging
import mimetypes
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_user
from harbor_clerk.api.schemas.uploads import (
    ConfirmUploadRequest,
    ConfirmUploadResponse,
    UploadFileResult,
    UploadResponse,
    UploadStatusResponse,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.storage import get_storage
from harbor_clerk.models import Document, DocumentVersion, Upload
from harbor_clerk.models.enums import JobStage, VersionStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])


@router.post("/uploads", response_model=UploadResponse)
async def upload_files(
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    max_file_bytes = settings.max_file_size_mb * 1024 * 1024
    results: list[UploadFileResult] = []

    for file in files:
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

        # Store at temp key
        temp_key = f"tmp/uploads/{uuid.uuid4()}/{file.filename or 'file'}"
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


@router.post("/uploads/confirm", response_model=ConfirmUploadResponse)
async def confirm_upload(
    body: ConfirmUploadRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()

    # Load upload
    try:
        upload_uuid = uuid.UUID(body.upload_id)
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

    if body.action == "new_document":
        # Create document family
        filename = upload.original_filename
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        doc = Document(title=title, canonical_filename=filename)
        session.add(doc)
        await session.flush()
        doc_id = doc.doc_id

    elif body.action == "new_version":
        if body.existing_doc_id is None:
            raise HTTPException(status_code=422, detail="existing_doc_id required for new_version")
        try:
            doc_id = uuid.UUID(body.existing_doc_id)
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

    # Create document version
    version = DocumentVersion(
        doc_id=doc_id,
        original_sha256=upload.sha256,
        original_bucket=settings.minio_bucket,
        original_object_key="",  # will be set after move
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        status=VersionStatus.queued,
    )
    session.add(version)
    await session.flush()

    # Move object to canonical path
    canonical_key = f"versions/{version.version_id}/{upload.original_filename}"
    storage = get_storage()
    storage.copy_and_delete(
        upload.minio_bucket, upload.minio_object_key,
        settings.minio_bucket, canonical_key,
    )
    version.original_object_key = canonical_key

    # Update upload record
    upload.doc_id = doc_id
    upload.version_id = version.version_id
    upload.status = "processing"

    await log_audit(
        session,
        user_id=principal.id if principal.type == "user" else None,
        action="confirm_upload",
        target_type="document_version",
        target_id=version.version_id,
        detail={"action": body.action, "doc_id": str(doc_id)},
    )
    await session.commit()

    # Enqueue extract stage (import here to avoid circular imports)
    from harbor_clerk.worker.pipeline import enqueue_stage
    enqueue_stage(version.version_id, JobStage.extract)

    return ConfirmUploadResponse(
        doc_id=str(doc_id),
        version_id=str(version.version_id),
        status="processing",
    )


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
