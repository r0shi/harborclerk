"""Watch folder API endpoints — CRUD, ingest, remove, rename."""

import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import get_session, require_human_user, require_user
from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.file_types import ALLOWED_EXTENSIONS, is_excalidraw
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus, UploadSource
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.upload import Upload
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.notify import notify_folder_change_async
from harbor_clerk.worker.pipeline import enqueue_stage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watch", tags=["watch"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class FolderCreate(BaseModel):
    path: str
    bookmark_data: str | None = None  # base64-encoded; legacy macOS clients only
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
        "display_name": f.display_name,
        "auto_discovered": f.auto_discovered,
        "unavailable_reason": f.unavailable_reason,
    }


def _parse_uuid(value: str, field: str = "id") -> uuid.UUID:
    """Parse a UUID string, raising 400 on invalid input."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid UUID for {field}: {value!r}")


def _parse_hex(value: str, field: str = "sha256") -> bytes:
    """Parse a hex string to bytes, raising 400 on invalid input."""
    try:
        return bytes.fromhex(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid hex for {field}: {value!r}")


def _parse_base64(value: str, field: str = "bookmark_data") -> bytes:
    """Parse a base64 string to bytes, raising 400 on invalid input."""
    try:
        return base64.b64decode(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid base64 for {field}")


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


@router.get("/system")
async def get_system(_: None = Depends(require_user)):
    """Platform-shape introspection for the frontend.

    Empty WATCH_ROOT → macOS native deployment (native folder picker).
    Non-empty WATCH_ROOT → Docker deployment (folders are mounted, no picker).
    """
    settings = get_settings()
    is_docker = bool(settings.watch_root)
    return {
        "platform": "docker" if is_docker else "macos",
        "picker": "none" if is_docker else "native",
        "watch_root": settings.watch_root or None,
    }


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


async def _folder_progress_event_generator(session_factory=None):
    """Async generator yielding SSE `data:` lines for per-folder progress deltas.

    1Hz polling loop. Emits an initial snapshot for every folder on the first
    iteration (because `prev` starts empty), then only emits when a folder's
    snapshot has changed. Cleanly handles cancellation via CancelledError.

    Extracted as a module-level coroutine so tests can drive it directly
    without round-tripping through the (response-buffering) ASGI test transport.
    Tests inject their own session factory to avoid binding the module-level
    engine to a transient test event loop.
    """
    if session_factory is None:
        session_factory = async_session_factory
    prev: dict[str, tuple[int, int, str]] = {}
    try:
        while True:
            async with session_factory() as sess:
                # Per-folder counts: total active files + finalize-done jobs
                stmt = (
                    select(
                        WatchedFolder.folder_id,
                        WatchedFolder.last_scan_at,
                        func.count(WatchedFile.file_id)
                        .filter(WatchedFile.status == WatchedFileStatus.active)
                        .label("total"),
                        func.count(IngestionJob.job_id)
                        .filter(
                            IngestionJob.stage == JobStage.finalize,
                            IngestionJob.status == JobStatus.done,
                        )
                        .label("done"),
                    )
                    .outerjoin(WatchedFile, WatchedFile.folder_id == WatchedFolder.folder_id)
                    .outerjoin(IngestionJob, IngestionJob.doc_id == WatchedFile.doc_id)
                    .group_by(WatchedFolder.folder_id, WatchedFolder.last_scan_at)
                )
                rows = (await sess.execute(stmt)).all()

            for fid, last_scan, total, done in rows:
                scan_status = "scanning" if last_scan is None else "idle"
                snap = (total or 0, done or 0, scan_status)
                fid_str = str(fid)
                if prev.get(fid_str) != snap:
                    prev[fid_str] = snap
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "folder_id": fid_str,
                                "total_files": snap[0],
                                "completed_files": snap[1],
                                "scan_status": snap[2],
                            }
                        )
                        + "\n\n"
                    )

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return


@router.get("/folders/stream")
async def folder_progress_stream(_: None = Depends(require_user)):
    """SSE stream of per-folder progress deltas.

    1Hz polling loop. Emits initial snapshot for all folders on connect,
    then only deltas. Cleanly handles client disconnect via CancelledError.
    """
    return StreamingResponse(
        _folder_progress_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/folders/{folder_id}/progress")
async def get_folder_progress(
    folder_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_user),
):
    """Aggregate per-folder ingestion status across all 7 pipeline stages."""
    folder = await session.get(WatchedFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    total = await session.scalar(
        select(func.count())
        .select_from(WatchedFile)
        .where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )

    stages = (
        JobStage.extract,
        JobStage.ocr,
        JobStage.chunk,
        JobStage.entities,
        JobStage.embed,
        JobStage.summarize,
        JobStage.finalize,
    )
    by_stage: dict[str, dict[str, int]] = {s.value: {"pending": 0, "running": 0, "done": 0, "error": 0} for s in stages}

    result = await session.execute(
        select(IngestionJob.stage, IngestionJob.status, func.count())
        .join(WatchedFile, WatchedFile.doc_id == IngestionJob.doc_id)
        .where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.status == WatchedFileStatus.active,
        )
        .group_by(IngestionJob.stage, IngestionJob.status)
    )
    # JobStatus.queued -> "pending" in the response (frontend semantics)
    status_map = {
        JobStatus.queued: "pending",
        JobStatus.running: "running",
        JobStatus.done: "done",
        JobStatus.error: "error",
    }
    for stage, status, count in result.all():
        if stage.value in by_stage and status in status_map:
            by_stage[stage.value][status_map[status]] = count

    completed = by_stage[JobStage.finalize.value]["done"]

    return {
        "total_files": total or 0,
        "completed_files": completed,
        "by_stage": by_stage,
        "scan_status": "scanning" if folder.last_scan_at is None else "idle",
        "last_scan_at": folder.last_scan_at.isoformat() if folder.last_scan_at else None,
    }


@router.post("/folders", status_code=201)
async def create_folder(
    body: FolderCreate,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    settings = get_settings()
    normalized = os.path.realpath(body.path)

    if settings.watch_root:
        # Docker: confine the user-supplied path to immediate children of
        # WATCH_ROOT BEFORE any filesystem access, so a malicious request
        # can't probe arbitrary paths via the validation responses.
        root = os.path.realpath(settings.watch_root)
        if os.path.dirname(normalized) != root:
            raise HTTPException(
                status_code=400,
                detail="Path must be a top-level subdirectory of WATCH_ROOT",
            )
        # Now safe to touch the filesystem — `normalized` is bounded to root/*.
        if not Path(normalized).is_dir():
            raise HTTPException(
                status_code=400,
                detail="Path must be a top-level subdirectory of WATCH_ROOT",
            )
    else:
        # macOS native deployment: this endpoint requires an authenticated
        # human admin (`require_human_user`). Admins are trusted to specify
        # their own filesystem paths — same trust level as ssh-ing into the
        # box. The is_dir / access checks below operate on user-controlled
        # input by design.
        p = Path(normalized)
        if not p.is_dir() or not os.access(p, os.R_OK):  # codeql[py/path-injection-pre-validated]
            raise HTTPException(
                status_code=400,
                detail="Path is not a readable directory",
            )

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

    # bookmark_data is optional; ignored on Docker; legacy macOS clients can still send it.
    bookmark_bytes: bytes | None = None
    if body.bookmark_data and not settings.watch_root:
        bookmark_bytes = _parse_base64(body.bookmark_data, "bookmark_data")

    folder = WatchedFolder(
        path=normalized,
        bookmark_data=bookmark_bytes,
        recursive=body.recursive,
        display_name=Path(normalized).name,
    )
    session.add(folder)
    await session.flush()  # populate folder.folder_id

    # NOTIFY rides on the same transaction; fire BEFORE commit.
    await notify_folder_change_async(session, folder.folder_id, action="added")
    await session.commit()
    await session.refresh(folder)
    return _folder_to_dict(folder, 0)


@router.patch("/folders/{folder_id}")
async def patch_folder(
    folder_id: str,
    body: FolderPatch,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    result = await session.execute(
        select(WatchedFolder).where(WatchedFolder.folder_id == _parse_uuid(folder_id, "folder_id"))
    )
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    enabled_changed_to: bool | None = None
    if body.enabled is not None and body.enabled != folder.enabled:
        folder.enabled = body.enabled
        enabled_changed_to = body.enabled
    if body.last_event_id is not None:
        folder.last_event_id = body.last_event_id

    if enabled_changed_to is not None:
        action = "enabled" if enabled_changed_to else "disabled"
        await notify_folder_change_async(session, folder.folder_id, action=action)

    await session.commit()
    return {"status": "updated"}


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    fid = _parse_uuid(folder_id, "folder_id")
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    # Active Docker mounts cannot be deleted via the API — operator must unmount first.
    if folder.auto_discovered and folder.unavailable_reason is None:
        raise HTTPException(
            status_code=409,
            detail="Folder is an active Docker mount; unmount it first",
        )

    # Collect doc_ids linked to this folder's files
    files_result = await session.execute(
        select(WatchedFile.doc_id).where(WatchedFile.folder_id == fid, WatchedFile.doc_id.isnot(None))
    )
    doc_ids = [row[0] for row in files_result.all()]

    # Delete folder first (cascades to watched_files via FK)
    await session.execute(delete(WatchedFolder).where(WatchedFolder.folder_id == fid))

    # Delete linked documents via SQL DELETE (DB cascades handle chunks, jobs, etc.)
    # Using raw DELETE avoids SQLAlchemy ORM autoflush FK conflicts
    if doc_ids:
        await session.execute(delete(Document).where(Document.doc_id.in_(doc_ids)))

    # NOTIFY rides on the same transaction; fire BEFORE commit.
    await notify_folder_change_async(session, fid, action="removed")
    await session.commit()


@router.post("/folders/{folder_id}/rescan")
async def rescan_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    result = await session.execute(
        select(WatchedFolder).where(WatchedFolder.folder_id == _parse_uuid(folder_id, "folder_id"))
    )
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
    principal=Depends(require_human_user),
):
    fid = _parse_uuid(body.folder_id, "folder_id")
    result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Watched folder not found")

    # Security: validate source_path is within folder
    _validate_source_path(body.source_path, folder.path)

    # Validate extension
    if is_excalidraw(body.relative_path):
        raise HTTPException(status_code=400, detail="Excalidraw drawings are not ingested")
    ext = PurePosixPath(body.relative_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

    sha256_bytes = _parse_hex(body.sha256, "sha256")
    bookmark_bytes = _parse_base64(body.bookmark_data, "bookmark_data")
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
        return {"action": "skipped", "doc_id": str(wf.doc_id)}

    # Case 2: removed file at same path — resurrect (bookmark may differ after Trash restore)
    if wf and wf.status == WatchedFileStatus.removed:
        wf.status = WatchedFileStatus.active
        wf.removed_at = None
        wf.bookmark_data = bookmark_bytes  # refresh bookmark

        if wf.doc_id:
            doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
            doc = doc_result.scalar_one_or_none()
            if doc:
                doc.status = "active"
                doc.source_path = body.source_path

        if wf.sha256 != sha256_bytes:
            # SHA changed — re-ingest same document with updated content
            wf.sha256 = sha256_bytes
            if wf.doc_id:
                doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
                doc = doc_result.scalar_one_or_none()
                if doc:
                    doc.sha256 = sha256_bytes
                    doc.pipeline_status = PipelineStatus.queued
                    doc.pipeline_seq = (doc.pipeline_seq or 0) + 1
                    doc.source_path = body.source_path
                    doc.mime_type = body.mime_type
                    await session.commit()
                    await asyncio.to_thread(enqueue_stage, doc.doc_id, JobStage.extract, priority=10)
                    return {"action": "updated", "doc_id": str(doc.doc_id)}

        await session.commit()
        return {"action": "resurrected", "doc_id": str(wf.doc_id)}

    # Case 3: active + different SHA256 → re-ingest same document
    if wf and wf.status == WatchedFileStatus.active and wf.sha256 != sha256_bytes:
        wf.sha256 = sha256_bytes
        wf.bookmark_data = bookmark_bytes
        if wf.doc_id:
            doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
            doc = doc_result.scalar_one_or_none()
            if doc:
                doc.sha256 = sha256_bytes
                doc.pipeline_status = PipelineStatus.queued
                doc.pipeline_seq = (doc.pipeline_seq or 0) + 1
                doc.source_path = body.source_path
                doc.mime_type = body.mime_type
                await session.commit()
                await asyncio.to_thread(enqueue_stage, doc.doc_id, JobStage.extract, priority=10)
                return {"action": "updated", "doc_id": str(doc.doc_id)}

    # Case 4: new file
    doc = Document(
        title=PurePosixPath(filename).stem,
        canonical_filename=filename,
        status="active",
        sha256=sha256_bytes,
        mime_type=body.mime_type,
        source_path=body.source_path,
        pipeline_status=PipelineStatus.queued,
    )
    session.add(doc)
    await session.flush()

    upload = Upload(
        user_id=None,  # watched folder uploads are system-initiated, no real user
        source=UploadSource.watch_folder,
        original_filename=filename,
        mime_type=body.mime_type,
        sha256=sha256_bytes,
        minio_bucket="",
        minio_object_key="",
        doc_id=doc.doc_id,
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
        status=WatchedFileStatus.active,
    )
    session.add(new_wf)
    await session.commit()

    await asyncio.to_thread(enqueue_stage, doc.doc_id, JobStage.extract, priority=10)
    return {"action": "created", "doc_id": str(doc.doc_id)}


@router.post("/remove")
async def remove_file(
    body: RemoveRequest,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    fid = _parse_uuid(body.folder_id, "folder_id")
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
    _: None = Depends(require_human_user),
):
    fid = _parse_uuid(body.folder_id, "folder_id")

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
    wf.bookmark_data = _parse_base64(body.bookmark_data, "bookmark_data")

    # Update source_path on the document
    if wf.doc_id:
        doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
        doc = doc_result.scalar_one_or_none()
        if doc:
            doc.source_path = body.source_path

    await session.commit()
    return {"action": "renamed", "doc_id": str(wf.doc_id)}
