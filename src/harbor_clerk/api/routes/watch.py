"""Watch folder API endpoints — CRUD, ingest, remove, rename."""

import asyncio
import base64
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import get_session, require_user
from harbor_clerk.api.routes.uploads import ALLOWED_EXTENSIONS
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import JobStage, UploadSource, VersionStatus
from harbor_clerk.models.upload import Upload
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.worker.pipeline import enqueue_stage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watch", tags=["watch"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class FolderCreate(BaseModel):
    path: str
    bookmark_data: str  # base64-encoded
    recursive: bool = True


class FolderPatch(BaseModel):
    enabled: bool | None = None
    last_event_id: int | None = None


class IngestRequest(BaseModel):
    folder_id: str
    relative_path: str
    sha256: str  # hex
    bookmark_data: str  # base64
    source_path: str
    mime_type: str


class RemoveRequest(BaseModel):
    folder_id: str
    relative_path: str


class RenameRequest(BaseModel):
    folder_id: str
    old_relative_path: str
    new_relative_path: str
    bookmark_data: str  # base64
    source_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _folder_to_dict(f: WatchedFolder, file_count: int) -> dict:
    return {
        "folder_id": str(f.folder_id),
        "path": f.path,
        "recursive": f.recursive,
        "enabled": f.enabled,
        "last_event_id": f.last_event_id,
        "last_scan_at": f.last_scan_at.isoformat() if f.last_scan_at else None,
        "file_count": file_count,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


def _validate_source_path(source_path: str, folder_path: str) -> str:
    """Validate that source_path is within the folder. Returns realpath."""
    real_source = os.path.realpath(source_path)
    real_folder = os.path.realpath(folder_path)
    if not real_source.startswith(real_folder + os.sep):
        raise HTTPException(status_code=400, detail="source_path is outside the watched folder")
    return real_source


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/allowed-extensions")
async def allowed_extensions(_: None = Depends(require_user)):
    return sorted(ALLOWED_EXTENSIONS)


@router.get("/folders")
async def list_folders(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    count_sub = (
        select(func.count(WatchedFile.file_id))
        .where(
            WatchedFile.folder_id == WatchedFolder.folder_id,
            WatchedFile.status == WatchedFileStatus.active,
        )
        .correlate(WatchedFolder)
        .scalar_subquery()
    )
    result = await session.execute(select(WatchedFolder, count_sub))
    rows = result.all()
    return [_folder_to_dict(row[0], row[1] or 0) for row in rows]


@router.post("/folders", status_code=201)
async def create_folder(
    body: FolderCreate,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    normalized = os.path.realpath(body.path)

    # Check for overlapping folders
    existing = await session.execute(select(WatchedFolder))
    for (folder,) in existing.all():
        existing_real = os.path.realpath(folder.path)
        if normalized == existing_real:
            raise HTTPException(status_code=409, detail="This folder is already watched")
        if normalized.startswith(existing_real + os.sep):
            raise HTTPException(status_code=409, detail="This folder is inside an already-watched folder")
        if existing_real.startswith(normalized + os.sep):
            raise HTTPException(status_code=409, detail="An already-watched folder is inside this folder")

    folder = WatchedFolder(
        path=normalized,
        bookmark_data=base64.b64decode(body.bookmark_data),
        recursive=body.recursive,
    )
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return _folder_to_dict(folder, 0)


@router.patch("/folders/{folder_id}")
async def patch_folder(
    folder_id: str,
    body: FolderPatch,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == uuid.UUID(folder_id)))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    if body.enabled is not None:
        folder.enabled = body.enabled
    if body.last_event_id is not None:
        folder.last_event_id = body.last_event_id

    await session.commit()
    return {"status": "updated"}


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    fid = uuid.UUID(folder_id)
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    # Soft-delete linked documents
    files_result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == fid,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    for wf in files_result.scalars().all():
        if wf.doc_id:
            doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
            doc = doc_result.scalar_one_or_none()
            if doc:
                doc.status = "removed"

    # Cascade delete folder + files
    await session.execute(delete(WatchedFolder).where(WatchedFolder.folder_id == fid))
    await session.commit()


@router.post("/folders/{folder_id}/rescan")
async def rescan_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == uuid.UUID(folder_id)))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    folder.last_event_id = None
    await session.commit()
    return {"status": "rescan_requested"}


@router.post("/ingest")
async def ingest_file(
    body: IngestRequest,
    session: AsyncSession = Depends(get_session),
    principal=Depends(require_user),
):
    fid = uuid.UUID(body.folder_id)
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    # Security: validate source_path is within folder
    _validate_source_path(body.source_path, folder.path)

    # Validate extension
    ext = PurePosixPath(body.relative_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

    sha256_bytes = bytes.fromhex(body.sha256)
    bookmark_bytes = base64.b64decode(body.bookmark_data)
    filename = PurePosixPath(body.relative_path).name

    # Look up existing watched file
    wf_result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == fid,
            WatchedFile.relative_path == body.relative_path,
        )
    )
    wf = wf_result.scalar_one_or_none()

    # Case 1: active + same SHA256 → skip
    if wf and wf.status == WatchedFileStatus.active and wf.sha256 == sha256_bytes:
        return {"action": "skipped", "doc_id": str(wf.doc_id), "version_id": str(wf.version_id)}

    # Case 2: removed file
    if wf and wf.status == WatchedFileStatus.removed:
        if wf.bookmark_data == bookmark_bytes:
            # Resurrection — reactivate file and doc
            wf.status = WatchedFileStatus.active
            wf.removed_at = None
            wf.bookmark_data = bookmark_bytes

            if wf.doc_id:
                doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
                doc = doc_result.scalar_one_or_none()
                if doc:
                    doc.status = "active"

                if wf.version_id:
                    ver_result = await session.execute(
                        select(DocumentVersion).where(DocumentVersion.version_id == wf.version_id)
                    )
                    ver = ver_result.scalar_one_or_none()
                    if ver:
                        ver.source_path = body.source_path

            if wf.sha256 != sha256_bytes:
                # SHA changed — re-ingest with new version
                wf.sha256 = sha256_bytes
                version_id = await _create_new_version(
                    session, wf.doc_id, sha256_bytes, body.source_path, body.mime_type, filename
                )
                wf.version_id = version_id

                # Update doc latest_version_id
                doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
                doc = doc_result.scalar_one_or_none()
                if doc:
                    doc.latest_version_id = version_id

                await session.commit()
                await asyncio.to_thread(enqueue_stage, version_id, JobStage.extract, priority=10)
                return {"action": "updated", "doc_id": str(wf.doc_id), "version_id": str(version_id)}

            await session.commit()
            return {"action": "resurrected", "doc_id": str(wf.doc_id), "version_id": str(wf.version_id)}
        else:
            # Different bookmark — hard-delete old row, fall through to new-file
            await session.execute(delete(WatchedFile).where(WatchedFile.file_id == wf.file_id))
            await session.flush()
            wf = None

    # Case 3: active + different SHA256 → new version
    if wf and wf.status == WatchedFileStatus.active and wf.sha256 != sha256_bytes:
        version_id = await _create_new_version(
            session, wf.doc_id, sha256_bytes, body.source_path, body.mime_type, filename
        )
        # Update doc
        doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
        doc = doc_result.scalar_one_or_none()
        if doc:
            doc.latest_version_id = version_id

        wf.sha256 = sha256_bytes
        wf.version_id = version_id
        wf.bookmark_data = bookmark_bytes
        await session.commit()
        await asyncio.to_thread(enqueue_stage, version_id, JobStage.extract, priority=10)
        return {"action": "updated", "doc_id": str(wf.doc_id), "version_id": str(version_id)}

    # Case 4: new file
    doc = Document(title=PurePosixPath(filename).stem, canonical_filename=filename, status="active")
    session.add(doc)
    await session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=sha256_bytes,
        original_bucket=None,
        original_object_key=None,
        mime_type=body.mime_type,
        source_path=body.source_path,
        status=VersionStatus.queued,
    )
    session.add(version)
    await session.flush()

    doc.latest_version_id = version.version_id

    upload = Upload(
        user_id=principal.id if principal.type == "user" else None,
        source=UploadSource.watch_folder,
        original_filename=filename,
        mime_type=body.mime_type,
        sha256=sha256_bytes,
        minio_bucket="",
        minio_object_key="",
        doc_id=doc.doc_id,
        version_id=version.version_id,
        source_path=body.source_path,
        status="confirmed",
    )
    session.add(upload)

    new_wf = WatchedFile(
        folder_id=fid,
        relative_path=body.relative_path,
        bookmark_data=bookmark_bytes,
        sha256=sha256_bytes,
        doc_id=doc.doc_id,
        version_id=version.version_id,
        status=WatchedFileStatus.active,
    )
    session.add(new_wf)
    await session.commit()

    await asyncio.to_thread(enqueue_stage, version.version_id, JobStage.extract, priority=10)
    return {"action": "created", "doc_id": str(doc.doc_id), "version_id": str(version.version_id)}


@router.post("/remove")
async def remove_file(
    body: RemoveRequest,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    fid = uuid.UUID(body.folder_id)
    result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == fid,
            WatchedFile.relative_path == body.relative_path,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Watched file not found")

    wf.status = WatchedFileStatus.removed
    wf.removed_at = datetime.now(UTC)

    if wf.doc_id:
        doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
        doc = doc_result.scalar_one_or_none()
        if doc:
            doc.status = "removed"

    await session.commit()
    return {"action": "removed", "doc_id": str(wf.doc_id)}


@router.post("/rename")
async def rename_file(
    body: RenameRequest,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    fid = uuid.UUID(body.folder_id)

    # Look up folder for path validation
    folder_result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))
    folder = folder_result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    _validate_source_path(body.source_path, folder.path)

    result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == fid,
            WatchedFile.relative_path == body.old_relative_path,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Watched file not found")

    wf.relative_path = body.new_relative_path
    wf.bookmark_data = base64.b64decode(body.bookmark_data)

    # Update source_path on the version
    if wf.version_id:
        ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == wf.version_id))
        ver = ver_result.scalar_one_or_none()
        if ver:
            ver.source_path = body.source_path

    await session.commit()
    return {"action": "renamed", "doc_id": str(wf.doc_id)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _create_new_version(
    session: AsyncSession,
    doc_id: uuid.UUID,
    sha256: bytes,
    source_path: str,
    mime_type: str,
    filename: str,
) -> uuid.UUID:
    """Create a new DocumentVersion for an existing document."""
    version = DocumentVersion(
        doc_id=doc_id,
        original_sha256=sha256,
        original_bucket=None,
        original_object_key=None,
        mime_type=mime_type,
        source_path=source_path,
        status=VersionStatus.queued,
    )
    session.add(version)
    await session.flush()
    return version.version_id
