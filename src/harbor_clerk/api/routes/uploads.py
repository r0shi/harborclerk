"""Upload endpoints: upload files, confirm, list status."""

import asyncio
import hashlib
import io
import logging
import mimetypes
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_user
from harbor_clerk.api.schemas.uploads import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchConfirmResultItem,
    ConfirmUploadRequest,
    ConfirmUploadResponse,
    CreateSessionRequest,
    ResumeResponse,
    SessionFileUploadResponse,
    SessionResponse,
    UploadFileResult,
    UploadResponse,
    UploadStatusResponse,
)
from harbor_clerk.audit import log_audit
from harbor_clerk.config import Settings, get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models import Document, DocumentVersion, Upload, UploadSession
from harbor_clerk.models.enums import JobStage, VersionStatus
from harbor_clerk.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])

ALLOWED_EXTENSIONS = {
    # Documents
    ".pdf",
    ".docx",
    ".doc",
    ".rtf",
    ".txt",
    ".md",
    ".odt",
    ".pages",
    # Spreadsheets
    ".xlsx",
    ".xls",
    ".ods",
    ".numbers",
    ".csv",
    # Presentations
    ".pptx",
    ".ppt",
    ".odp",
    ".key",
    # Images (OCR)
    ".jpg",
    ".jpeg",
    ".png",
    ".tiff",
    ".tif",
    # eBooks
    ".epub",
    # Web
    ".html",
    ".htm",
    # Email
    ".eml",
}


@router.post("/uploads", response_model=UploadResponse)
async def upload_files(
    files: list[UploadFile] = File(..., max_part_size=200 * 1024 * 1024),
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
            results.append(
                UploadFileResult(
                    upload_id=str(uuid.uuid4()),
                    filename=fname or "unknown",
                    size_bytes=0,
                    mime_type=file.content_type or "application/octet-stream",
                    status="skipped",
                )
            )
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

        # Skip 0-byte entries (likely folder placeholders from drag-and-drop)
        if total_size == 0:
            results.append(
                UploadFileResult(
                    upload_id=str(uuid.uuid4()),
                    filename=fname or "unknown",
                    size_bytes=0,
                    mime_type=file.content_type or "application/octet-stream",
                    status="skipped",
                )
            )
            continue

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
            results.append(
                UploadFileResult(
                    upload_id=str(upload_row.upload_id),
                    filename=file.filename or "unknown",
                    size_bytes=total_size,
                    mime_type=mime,
                    status="duplicate",
                    duplicate_doc_id=str(dup_version.doc_id),
                    duplicate_version_id=str(dup_version.version_id),
                )
            )
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

        results.append(
            UploadFileResult(
                upload_id=str(upload_row.upload_id),
                filename=file.filename or "unknown",
                size_bytes=total_size,
                mime_type=mime,
                status="pending_confirmation",
            )
        )

    await session.commit()
    return UploadResponse(files=results)


async def _confirm_single(
    session: AsyncSession,
    upload_id_str: str,
    action: str,
    existing_doc_id: str | None,
    principal: Principal,
    settings: Settings,
    source_path: str | None = None,
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
    result = await session.execute(select(Upload).where(Upload.upload_id == upload_uuid))
    upload = result.scalar_one_or_none()
    if upload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    if upload.status != "pending_confirmation":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload status is '{upload.status}', not pending_confirmation",
        )

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
        source_path=source_path,
    )
    session.add(version)
    await session.flush()

    safe_name = os.path.basename(upload.original_filename) or "file"
    canonical_key = f"versions/{version.version_id}/{safe_name}"
    storage = get_storage()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        storage.copy_and_delete,
        upload.minio_bucket,
        upload.minio_object_key,
        settings.minio_bucket,
        canonical_key,
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
        session,
        body.upload_id,
        body.action,
        body.existing_doc_id,
        principal,
        settings,
        source_path=body.source_path,
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
                session,
                item.upload_id,
                item.action,
                item.existing_doc_id,
                principal,
                settings,
                source_path=item.source_path,
            )
            results.append(
                BatchConfirmResultItem(
                    upload_id=item.upload_id,
                    doc_id=confirm_result.doc_id,
                    version_id=confirm_result.version_id,
                    status=confirm_result.status,
                )
            )
            version_ids_to_enqueue.append(uuid.UUID(confirm_result.version_id))
        except HTTPException as exc:
            results.append(
                BatchConfirmResultItem(
                    upload_id=item.upload_id,
                    status="error",
                    error=exc.detail,
                )
            )

    await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage

    loop = asyncio.get_running_loop()

    def _enqueue_all():
        for vid in version_ids_to_enqueue:
            enqueue_stage(vid, JobStage.extract)

    await loop.run_in_executor(None, _enqueue_all)

    # Trigger topic recompute in background with its own session
    async def _recompute_topics_bg():
        from harbor_clerk.db import async_session_factory
        from harbor_clerk.topics import check_and_recompute_topics

        async with async_session_factory() as bg_session:
            await check_and_recompute_topics(bg_session)

    asyncio.create_task(_recompute_topics_bg())

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


# ── Upload Sessions ──────────────────────────────────────────────


def _session_response(s: UploadSession) -> SessionResponse:
    return SessionResponse(
        session_id=str(s.session_id),
        user_id=str(s.user_id),
        label=s.label,
        auto_confirm=s.auto_confirm,
        status=s.status,
        total_files=s.total_files,
        uploaded=s.uploaded,
        confirmed=s.confirmed,
        failed=s.failed,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _increment_session_counters(db: AsyncSession, session_id: uuid.UUID, **increments: int) -> None:
    """Atomically increment session counters via SQL UPDATE."""
    values = {k: getattr(UploadSession, k) + v for k, v in increments.items() if v}
    values["updated_at"] = datetime.now(UTC)
    await db.execute(update(UploadSession).where(UploadSession.session_id == session_id).values(**values))


async def _get_session_or_404(session_id_str: str, db: AsyncSession, principal: Principal) -> UploadSession:
    try:
        sid = uuid.UUID(session_id_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session_id")
    result = await db.execute(select(UploadSession).where(UploadSession.session_id == sid))
    us = result.scalar_one_or_none()
    if us is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if principal.type == "user" and us.user_id != principal.id:
        raise HTTPException(status_code=403, detail="Not your session")
    return us


@router.post("/uploads/sessions", response_model=SessionResponse)
async def create_session(
    body: CreateSessionRequest,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = UploadSession(
        user_id=principal.id,
        label=body.label,
        auto_confirm=body.auto_confirm,
        total_files=body.total_files,
    )
    db.add(us)
    await db.flush()
    await db.commit()
    await db.refresh(us)
    return _session_response(us)


@router.get("/uploads/sessions/{session_id}", response_model=SessionResponse)
async def get_upload_session(
    session_id: str,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = await _get_session_or_404(session_id, db, principal)
    return _session_response(us)


@router.post("/uploads/sessions/{session_id}/files", response_model=SessionFileUploadResponse)
async def upload_file_to_session(
    session_id: str,
    file: UploadFile = File(..., max_part_size=200 * 1024 * 1024),
    source_path: str | None = Form(default=None),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = await _get_session_or_404(session_id, db, principal)
    if us.status != "active":
        raise HTTPException(status_code=400, detail=f"Session status is '{us.status}', not active")

    # Validate source_path length to prevent abuse
    if source_path and len(source_path) > 1024:
        raise HTTPException(status_code=400, detail="source_path exceeds 1024 character limit")

    settings = get_settings()
    max_file_bytes = settings.max_file_size_mb * 1024 * 1024

    fname = file.filename or ""
    dot = fname.rfind(".")
    ext = fname[dot:].lower() if dot != -1 else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

    # Stream file, compute SHA256
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
                detail=f"File exceeds {settings.max_file_size_mb}MB limit",
            )

    if total_size == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    sha256_bytes = sha.digest()
    sha256_hex = sha.hexdigest()
    mime = file.content_type or mimetypes.guess_type(fname)[0] or "application/octet-stream"

    # Duplicate check
    dup_result = await db.execute(select(DocumentVersion).where(DocumentVersion.original_sha256 == sha256_bytes))
    dup_version = dup_result.scalar_one_or_none()

    if dup_version is not None:
        upload_row = Upload(
            user_id=principal.id if principal.type == "user" else None,
            session_id=us.session_id,
            source_path=source_path,
            original_filename=fname or "unknown",
            mime_type=mime,
            size_bytes=total_size,
            sha256=sha256_bytes,
            minio_bucket=settings.minio_bucket,
            minio_object_key="",
            doc_id=dup_version.doc_id,
            version_id=dup_version.version_id,
            status="duplicate",
        )
        db.add(upload_row)
        await _increment_session_counters(db, us.session_id, uploaded=1)
        await db.flush()
        await db.commit()
        return SessionFileUploadResponse(
            upload_id=str(upload_row.upload_id),
            source_path=source_path,
            status="duplicate",
            sha256=sha256_hex,
            filename=fname,
            size_bytes=total_size,
            duplicate_doc_id=str(dup_version.doc_id),
            duplicate_version_id=str(dup_version.version_id),
        )

    if us.auto_confirm:
        # Auto-confirm: create document + version immediately
        safe_name = os.path.basename(fname) or "file"
        title = safe_name.rsplit(".", 1)[0] if "." in safe_name else safe_name
        doc = Document(title=title, canonical_filename=safe_name)
        db.add(doc)
        await db.flush()

        version = DocumentVersion(
            doc_id=doc.doc_id,
            original_sha256=sha256_bytes,
            original_bucket=settings.minio_bucket,
            original_object_key="",
            mime_type=mime,
            size_bytes=total_size,
            status=VersionStatus.queued,
            source_path=source_path,
        )
        db.add(version)
        await db.flush()

        canonical_key = f"versions/{version.version_id}/{safe_name}"
        content = b"".join(chunks)
        storage = get_storage()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, storage.put_object, settings.minio_bucket, canonical_key, io.BytesIO(content), total_size, mime
        )
        version.original_object_key = canonical_key

        upload_row = Upload(
            user_id=principal.id if principal.type == "user" else None,
            session_id=us.session_id,
            source_path=source_path,
            original_filename=fname or "unknown",
            mime_type=mime,
            size_bytes=total_size,
            sha256=sha256_bytes,
            minio_bucket=settings.minio_bucket,
            minio_object_key=canonical_key,
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status="processing",
        )
        db.add(upload_row)
        await _increment_session_counters(db, us.session_id, uploaded=1, confirmed=1)

        await log_audit(
            db,
            user_id=principal.id if principal.type == "user" else None,
            action="confirm_upload",
            target_type="document_version",
            target_id=version.version_id,
            detail={"action": "new_document", "doc_id": str(doc.doc_id), "session_id": str(us.session_id)},
        )
        await db.commit()

        # Enqueue extraction outside the session
        from harbor_clerk.worker.pipeline import enqueue_stage

        await loop.run_in_executor(None, enqueue_stage, version.version_id, JobStage.extract)

        return SessionFileUploadResponse(
            upload_id=str(upload_row.upload_id),
            source_path=source_path,
            status="processing",
            sha256=sha256_hex,
            filename=fname,
            size_bytes=total_size,
            doc_id=str(doc.doc_id),
            version_id=str(version.version_id),
        )
    else:
        # Review mode: store to temp location
        safe_name = os.path.basename(fname) or "file"
        temp_key = f"tmp/uploads/{uuid.uuid4()}/{safe_name}"
        content = b"".join(chunks)
        storage = get_storage()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, storage.put_object, settings.minio_bucket, temp_key, io.BytesIO(content), total_size, mime
        )

        upload_row = Upload(
            user_id=principal.id if principal.type == "user" else None,
            session_id=us.session_id,
            source_path=source_path,
            original_filename=fname or "unknown",
            mime_type=mime,
            size_bytes=total_size,
            sha256=sha256_bytes,
            minio_bucket=settings.minio_bucket,
            minio_object_key=temp_key,
            status="pending_confirmation",
        )
        db.add(upload_row)
        await _increment_session_counters(db, us.session_id, uploaded=1)
        await db.flush()
        await db.commit()

        return SessionFileUploadResponse(
            upload_id=str(upload_row.upload_id),
            source_path=source_path,
            status="pending_confirmation",
            sha256=sha256_hex,
            filename=fname,
            size_bytes=total_size,
        )


@router.post("/uploads/sessions/{session_id}/confirm", response_model=BatchConfirmResponse)
async def confirm_session(
    session_id: str,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = await _get_session_or_404(session_id, db, principal)
    if us.auto_confirm:
        raise HTTPException(status_code=400, detail="Session uses auto-confirm, no manual confirmation needed")

    # Find all pending uploads in this session
    result = await db.execute(
        select(Upload).where(Upload.session_id == us.session_id, Upload.status == "pending_confirmation")
    )
    pending = result.scalars().all()

    if not pending:
        raise HTTPException(status_code=400, detail="No pending files to confirm")

    settings = get_settings()
    results: list[BatchConfirmResultItem] = []
    version_ids_to_enqueue: list[uuid.UUID] = []

    confirmed_count = 0
    failed_count = 0
    for upload in pending:
        try:
            confirm_result = await _confirm_single(
                db,
                str(upload.upload_id),
                "new_document",
                None,
                principal,
                settings,
                source_path=upload.source_path,
            )
            results.append(
                BatchConfirmResultItem(
                    upload_id=str(upload.upload_id),
                    doc_id=confirm_result.doc_id,
                    version_id=confirm_result.version_id,
                    status=confirm_result.status,
                )
            )
            version_ids_to_enqueue.append(uuid.UUID(confirm_result.version_id))
            confirmed_count += 1
        except HTTPException as exc:
            results.append(
                BatchConfirmResultItem(
                    upload_id=str(upload.upload_id),
                    status="error",
                    error=exc.detail,
                )
            )
            failed_count += 1

    await db.execute(
        update(UploadSession)
        .where(UploadSession.session_id == us.session_id)
        .values(
            confirmed=UploadSession.confirmed + confirmed_count,
            failed=UploadSession.failed + failed_count,
            status="completed",
            updated_at=datetime.now(UTC),
        )
    )
    await db.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage

    loop = asyncio.get_running_loop()

    def _enqueue_all():
        for vid in version_ids_to_enqueue:
            enqueue_stage(vid, JobStage.extract)

    await loop.run_in_executor(None, _enqueue_all)

    # Trigger topic recompute in background with its own session
    async def _recompute_topics_bg2():
        from harbor_clerk.db import async_session_factory
        from harbor_clerk.topics import check_and_recompute_topics

        async with async_session_factory() as bg_session:
            await check_and_recompute_topics(bg_session)

    asyncio.create_task(_recompute_topics_bg2())

    return BatchConfirmResponse(results=results)


@router.delete("/uploads/sessions/{session_id}")
async def cancel_session(
    session_id: str,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = await _get_session_or_404(session_id, db, principal)
    if us.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Session already {us.status}")

    settings = get_settings()
    storage = get_storage()

    # Delete temp files for pending uploads
    result = await db.execute(
        select(Upload).where(Upload.session_id == us.session_id, Upload.status == "pending_confirmation")
    )
    pending = result.scalars().all()
    for upload in pending:
        if upload.minio_object_key and upload.minio_object_key.startswith("tmp/"):
            try:
                storage.remove_object(settings.minio_bucket, upload.minio_object_key)
            except Exception:
                logger.warning("Failed to delete temp file %s", upload.minio_object_key)
        upload.status = "cancelled"

    us.status = "cancelled"
    us.updated_at = datetime.now(UTC)
    await db.commit()
    return {"status": "cancelled"}


@router.get("/uploads/sessions/{session_id}/resume", response_model=ResumeResponse)
async def get_resume_info(
    session_id: str,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    us = await _get_session_or_404(session_id, db, principal)

    # Return source_paths of all successfully uploaded files
    result = await db.execute(
        select(Upload.source_path).where(
            Upload.session_id == us.session_id,
            Upload.source_path.isnot(None),
            Upload.status.in_(["pending_confirmation", "processing", "duplicate"]),
        )
    )
    paths = [row[0] for row in result.all()]
    return ResumeResponse(completed_paths=paths)
