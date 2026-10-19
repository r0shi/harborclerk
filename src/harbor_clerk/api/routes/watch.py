"""Watch folder API endpoints — CRUD, ingest, remove, rename."""

import asyncio
import base64
import json
import logging
import os
import posixpath
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Every route here is human-only: folder rows carry absolute on-disk paths and
# WATCH_ROOT is one, and no MCP tool exposes watched folders to API keys.
from harbor_clerk.api.deps import get_session, require_human_user
from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.file_types import ALLOWED_EXTENSIONS, is_excalidraw
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus, UploadSource
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.upload import Upload
from harbor_clerk.models.watched import (
    WATCHER_UNAVAILABLE_REASONS,
    WatchedFile,
    WatchedFileStatus,
    WatchedFolder,
    folder_is_unavailable,
)
from harbor_clerk.watched_source import (
    canonical_source_path,
    failed_for_a_missing_source,
    is_file_exact_case,
    repaired_source_path,
    watched_state_async,
)
from harbor_clerk.watcher.notify import notify_folder_change_async
from harbor_clerk.watcher.relocate import apply_move, move_needs_reextract
from harbor_clerk.watcher.visibility import restore_document, retire_watched_file
from harbor_clerk.worker.pipeline import queue_extract_for_current_pipeline, reset_and_queue_extract_for_doc

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
    display_name: str | None = None


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
        "skipped_count": f.skipped_count,
        "skipped_extensions": list(f.skipped_extensions or []),
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


def _contained_path(real_root: str, relative_path: str, field: str) -> str:
    """The realpath of ``relative_path`` under ``real_root`` (the folder's realpath), or a 400.

    Checked before any filesystem call: a relative path that is empty, absolute
    (``os.path.join`` would discard the root), has a ``..`` component or is not
    already normal (``a/./b``, ``a//b``) is refused; so is one whose realpath (a
    symlink inside the folder pointing elsewhere) leaves the folder.
    """
    if (
        not relative_path
        or "\0" in relative_path
        or os.path.isabs(relative_path)
        or ".." in relative_path.split("/")
        or posixpath.normpath(relative_path) != relative_path
    ):
        raise HTTPException(status_code=400, detail=f"{field} must be a plain path inside the watched folder")
    candidate = os.path.realpath(os.path.join(real_root, relative_path))
    if not candidate.startswith(real_root + os.sep):
        raise HTTPException(status_code=400, detail=f"{field} is outside the watched folder")
    return candidate


def _validate_source_path(source_path: str, folder_path: str) -> str:
    """Validate that source_path is within the folder. Returns realpath."""
    real_source = os.path.realpath(source_path)
    real_folder = os.path.realpath(folder_path)
    if not real_source.startswith(real_folder + os.sep):
        raise HTTPException(status_code=400, detail="source_path is outside the watched folder")
    return real_source


async def _bring_up_to_date(session: AsyncSession, doc: Document, sha256_bytes: bytes) -> None:
    """Queue the document's extract again, as the watcher does
    (``watcher/events.py::_bring_up_to_date``), when its last run failed because its
    file could not be read, or when its sha256 is not the watched file's (a #859-
    shaped new version indexed an upload); and only when it verifies as read in
    place, so one whose repair was declined is left as it is. The document takes the
    watched file's sha256, so the next visit finds it current.
    """
    stale = doc.sha256 != sha256_bytes
    if not stale and not failed_for_a_missing_source(doc):
        return
    if (await watched_state_async(session, doc)).path is None:
        return
    doc.sha256 = sha256_bytes
    await reset_and_queue_extract_for_doc(session, doc.doc_id, priority=10)


def _canonical_source_path(source_path: str, relative_path: str, folder_path: str) -> str:
    """The ``source_path`` to store for a watched file: the folder's path joined with
    its relative path, the form the watcher stores and ``watched_source`` verifies.

    The client's ``source_path`` must name that same file inside the folder: an alias
    (``/var`` vs ``/private/var``, a ``./`` segment, another Unicode normalization) is
    accepted and canonicalized; a different file, or a relative path that is not
    plain, is a 400.
    """
    real_source = _validate_source_path(source_path, folder_path)
    canonical = canonical_source_path(folder_path, relative_path)
    if canonical is None:
        raise HTTPException(status_code=400, detail="relative_path must be a plain path inside the watched folder")
    real_folder = os.path.realpath(folder_path)
    real_canonical = os.path.realpath(canonical)
    if not real_canonical.startswith(real_folder + os.sep):
        raise HTTPException(status_code=400, detail="relative_path is outside the watched folder")
    if real_canonical != real_source:
        try:
            same = os.path.samefile(real_canonical, real_source)
        except OSError:
            same = False
        if not same:
            raise HTTPException(status_code=400, detail="source_path and relative_path name different files")
    return canonical


def _rename_paths(folder_path: str, old_relative_path: str, new_relative_path: str, source_path: str):
    """``(old_rel, new_rel, real_new, canonical source_path)`` for a rename, or a 400.

    Both relative paths must be plain and stay inside the folder's realpath, and
    ``source_path`` must name the new file; an alias of it is accepted and the
    canonical path returned (``_canonical_source_path``). ``old_rel`` and
    ``new_rel`` are spelled as under the folder's realpath, for
    ``relocate.move_needs_reextract``.
    """
    real_root = os.path.realpath(folder_path)
    real_old = _contained_path(real_root, old_relative_path, "old_relative_path")
    real_new = _contained_path(real_root, new_relative_path, "new_relative_path")
    real_source = _validate_source_path(source_path, folder_path)
    if real_new != real_source:
        try:
            same = os.path.samefile(real_new, real_source)
        except OSError:
            same = False
        if not same:
            raise HTTPException(status_code=400, detail="source_path and new_relative_path name different files")
    canonical = _canonical_source_path(source_path, new_relative_path, folder_path)
    return real_old[len(real_root) + 1 :], real_new[len(real_root) + 1 :], real_new, canonical


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/allowed-extensions")
async def allowed_extensions(_: None = Depends(require_human_user)):
    return sorted(ALLOWED_EXTENSIONS)


@router.get("/system")
async def get_system(_: None = Depends(require_human_user)):
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
    _: None = Depends(require_human_user),
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
    prev: dict[str, tuple[int, int, str, str]] = {}
    try:
        while True:
            async with session_factory() as sess:
                # Per-folder counts: total active files + finalize-done jobs +
                # in-flight (queued OR running) jobs across any stage. The
                # in_flight count drives the "processing" pill on the frontend.
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
                        func.count(IngestionJob.job_id)
                        .filter(IngestionJob.status.in_((JobStatus.queued, JobStatus.running)))
                        .label("in_flight"),
                    )
                    .outerjoin(WatchedFile, WatchedFile.folder_id == WatchedFolder.folder_id)
                    .outerjoin(
                        Document,
                        and_(
                            Document.doc_id == WatchedFile.doc_id,
                            Document.status == "active",
                        ),
                    )
                    .outerjoin(
                        IngestionJob,
                        and_(
                            IngestionJob.doc_id == WatchedFile.doc_id,
                            IngestionJob.pipeline_seq == Document.pipeline_seq,
                        ),
                    )
                    .group_by(WatchedFolder.folder_id, WatchedFolder.last_scan_at)
                )
                rows = (await sess.execute(stmt)).all()

            for fid, last_scan, total, done, in_flight in rows:
                scan_status = "scanning" if last_scan is None else "idle"
                ingest_status = "processing" if (in_flight or 0) > 0 else "idle"
                snap = (total or 0, done or 0, scan_status, ingest_status)
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
                                "ingest_status": snap[3],
                            }
                        )
                        + "\n\n"
                    )

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return


@router.get("/folders/stream")
async def folder_progress_stream(_: None = Depends(require_human_user)):
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
    _: None = Depends(require_human_user),
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
        .join(Document, Document.doc_id == WatchedFile.doc_id)
        .where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.status == WatchedFileStatus.active,
            Document.status == "active",
            IngestionJob.pipeline_seq == Document.pipeline_seq,
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
    # A folder is "processing" while any stage has work queued or running for one
    # of its files; otherwise "idle". scan_status (the filesystem walk) is
    # independent of this — both can be true at once on a first-add.
    in_flight = sum(counts["pending"] + counts["running"] for counts in by_stage.values())

    return {
        "total_files": total or 0,
        "completed_files": completed,
        "by_stage": by_stage,
        "scan_status": "scanning" if folder.last_scan_at is None else "idle",
        "ingest_status": "processing" if in_flight > 0 else "idle",
        "last_scan_at": folder.last_scan_at.isoformat() if folder.last_scan_at else None,
    }


@router.post("/folders", status_code=201)
async def create_folder(
    body: FolderCreate,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_human_user),
):
    settings = get_settings()
    if settings.watch_root:
        # Docker: every top-level subdirectory of WATCH_ROOT is registered by the
        # watcher's discovery loop. A folder added here as well became a second
        # row for the same path (two observers, two documents per file), and a
        # path outside WATCH_ROOT is not mounted in the workers. Refused before
        # any filesystem access, so the response reveals nothing about the path.
        raise HTTPException(
            status_code=409,
            detail=(
                "Folders are discovered automatically on Docker: every top-level "
                "subdirectory of WATCH_ROOT is watched. Mount or create the folder there instead."
            ),
        )

    # macOS native deployment: this endpoint requires an authenticated
    # human admin (`require_human_user`). Admins are trusted to specify
    # their own filesystem paths — same trust level as ssh-ing into the
    # box. The is_dir / access checks below operate on user-controlled
    # input by design.
    normalized = os.path.realpath(body.path)
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

    # bookmark_data is optional; legacy macOS clients can still send it.
    bookmark_bytes: bytes | None = None
    if body.bookmark_data:
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
        if not body.enabled and folder.unavailable_reason in WATCHER_UNAVAILABLE_REASONS:
            # The watcher's notes describe passes over a folder it no longer
            # watches; it never sets one on a disabled folder. Discovery's
            # "unmounted" is not the API's to clear.
            folder.unavailable_reason = None
    if body.last_event_id is not None:
        folder.last_event_id = body.last_event_id
    if body.display_name is not None:
        new_name = body.display_name.strip()
        if not new_name:
            new_name = Path(folder.path).name
        if len(new_name) > 200:
            raise HTTPException(status_code=422, detail="display_name exceeds 200 chars")
        folder.display_name = new_name

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
    if folder.auto_discovered and not folder_is_unavailable(folder.unavailable_reason):
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

    # Validate extension
    if is_excalidraw(body.relative_path):
        raise HTTPException(status_code=400, detail="Excalidraw drawings are not ingested")
    ext = PurePosixPath(body.relative_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")
    # Stored in the canonical form, so the document verifies as read in place.
    source_path = await asyncio.to_thread(_canonical_source_path, body.source_path, body.relative_path, folder.path)

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

    # Case 1: active + same SHA256 → skip, but for a source_path that is not the
    # canonical path, which is repaired, and for a document whose index does not
    # reflect its file, which is queued again (_bring_up_to_date).
    if wf and wf.status == WatchedFileStatus.active and wf.sha256 == sha256_bytes:
        if wf.doc_id:
            doc = (await session.execute(select(Document).where(Document.doc_id == wf.doc_id))).scalar_one_or_none()
            if doc is not None:
                repaired = await asyncio.to_thread(
                    repaired_source_path, doc.source_path, folder.path, body.relative_path
                )
                if repaired is not None:
                    doc.source_path = repaired
                await _bring_up_to_date(session, doc, sha256_bytes)
                await session.commit()
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
                # As the watcher's resurrection: an admin's "deleted" stays deleted.
                restore_document(doc)
                doc.source_path = source_path

        if wf.sha256 != sha256_bytes:
            # SHA changed — re-ingest same document with updated content
            wf.sha256 = sha256_bytes
            if wf.doc_id:
                doc_result = await session.execute(select(Document).where(Document.doc_id == wf.doc_id))
                doc = doc_result.scalar_one_or_none()
                if doc:
                    doc.sha256 = sha256_bytes
                    doc.source_path = source_path
                    doc.mime_type = body.mime_type
                    await reset_and_queue_extract_for_doc(session, doc.doc_id, priority=10)
                    await session.commit()
                    return {"action": "updated", "doc_id": str(doc.doc_id)}

        # Same bytes: a document whose index does not reflect its file is queued
        # again (_bring_up_to_date); otherwise its index is current.
        if wf.doc_id:
            doc = (await session.execute(select(Document).where(Document.doc_id == wf.doc_id))).scalar_one_or_none()
            if doc is not None:
                await _bring_up_to_date(session, doc, sha256_bytes)
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
                doc.source_path = source_path
                doc.mime_type = body.mime_type
                await reset_and_queue_extract_for_doc(session, doc.doc_id, priority=10)
                await session.commit()
                return {"action": "updated", "doc_id": str(doc.doc_id)}

    # Case 4: new file
    doc = Document(
        title=PurePosixPath(filename).stem,
        canonical_filename=filename,
        status="active",
        sha256=sha256_bytes,
        mime_type=body.mime_type,
        source_path=source_path,
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
        source_path=source_path,
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
    await queue_extract_for_current_pipeline(session, doc.doc_id, priority=10)
    await session.commit()
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

    # A remove for a regular file still present under that exact spelling, every
    # component below the folder (an atomic save's trailing delete), is not a
    # removal: a removed watched file is unreadable. The same check as the
    # watcher's delete (watched_source.is_file_exact_case).
    folder = (await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == fid))).scalar_one_or_none()
    canonical = canonical_source_path(folder.path, body.relative_path) if folder is not None else None
    if canonical is not None and await asyncio.to_thread(is_file_exact_case, canonical, folder.path):
        return {"action": "skipped", "doc_id": str(wf.doc_id)}

    # The same retirement as a file deleted from disk (watcher/visibility.py, #604).
    doc = await session.get(Document, wf.doc_id) if wf.doc_id else None
    retire_watched_file(wf, doc)

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

    # Every path is validated before any other filesystem call (CodeQL
    # py/path-injection), off the event loop: both relative paths must stay inside
    # the folder, and the new one must be the file source_path names, which is
    # stored in its canonical form (folder path + relative path) so the document
    # verifies as read in place.
    old_rel, new_rel, real_new, source_path = await asyncio.to_thread(
        _rename_paths, folder.path, body.old_relative_path, body.new_relative_path, body.source_path
    )

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

    bookmark = _parse_base64(body.bookmark_data, "bookmark_data")

    # The same rules as the watcher's moved event (watcher/events.py::_handle_moved, #847).
    if body.new_relative_path != body.old_relative_path:
        dest_result = await session.execute(
            select(WatchedFile).where(
                WatchedFile.folder_id == fid,
                WatchedFile.relative_path == body.new_relative_path,
            )
        )
        dest = dest_result.scalar_one_or_none()
        if dest is not None:
            # A rename onto a path the index holds a row for (a live file it
            # replaced, or one retired earlier): the destination keeps its
            # document, the one its path cites, back in search if it was
            # retired, and takes the moved content (reprocessed when it
            # differs); the source is retired as a deleted file. Nothing is
            # deleted: the retired source can still be adopted.
            retire_watched_file(wf, await session.get(Document, wf.doc_id) if wf.doc_id else None)
            dest.bookmark_data = bookmark
            dest_doc = await session.get(Document, dest.doc_id) if dest.doc_id else None
            if dest.status == WatchedFileStatus.removed:
                dest.status = WatchedFileStatus.active
                dest.removed_at = None
                restore_document(dest_doc)
                await session.flush()  # the requeue below is raw SQL that only touches an active doc
            changed = dest.sha256 != wf.sha256
            dest.sha256 = wf.sha256
            if dest_doc is not None:
                dest_doc.source_path = source_path
                if changed:
                    dest_doc.sha256 = wf.sha256
                    await reset_and_queue_extract_for_doc(session, dest_doc.doc_id, priority=10)
            await session.commit()
            return {"action": "replaced", "doc_id": str(dest.doc_id)}

    doc = await session.get(Document, wf.doc_id) if wf.doc_id else None
    reextract = await asyncio.to_thread(move_needs_reextract, old_rel, new_rel, doc, real_new)
    apply_move(wf, doc, body.new_relative_path, source_path)
    wf.bookmark_data = bookmark
    if doc is not None and reextract:
        await reset_and_queue_extract_for_doc(session, doc.doc_id, priority=10)

    await session.commit()
    return {"action": "renamed", "doc_id": str(wf.doc_id)}
