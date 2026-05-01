"""Filesystem-event → database-write translation.

Pure module: no watchdog imports, no I/O scheduling. Caller passes in
synthetic FileEvent records; this module does the database work in the
provided session. Caller is responsible for commit.
"""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.chunk import Chunk
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_heading import DocumentHeading
from harbor_clerk.models.document_page import DocumentPage
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

logger = logging.getLogger(__name__)

# Kept in sync with ALLOWED_EXTENSIONS in harbor_clerk/api/routes/uploads.py.
# Duplicated here to avoid a transitive import chain through the API layer.
_ALLOWED_EXTENSIONS = {
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

# SHA-256 of empty content. Every 0-byte file produces this digest,
# so it's NOT a unique fingerprint and must never be used as a dedup
# match target — otherwise two transiently-empty files (think: network-share
# copy mid-transfer) would race into the same Document.
# See the early "skip 0-byte files" check in `handle_event` below.
_EMPTY_FILE_SHA256 = hashlib.sha256(b"").digest()


class EventKind(str, Enum):
    created = "created"
    modified = "modified"
    deleted = "deleted"


@dataclass
class FileEvent:
    kind: EventKind
    folder_id: uuid.UUID
    relative_path: str
    absolute_path: str


def _should_ignore(relative_path: str) -> bool:
    """Filter filesystem noise that should never be ingested.

    Catches:
      - AppleDouble shadow files (`._<name>`) created when macOS writes
        to non-Mac filesystems (network shares, FAT, exFAT, NFS, SMB).
        These have a real extension like `.pdf` so an extension-only
        filter would let them through.
      - macOS metadata files / directories: `.DS_Store`,
        `.Spotlight-V100`, `.Trashes`, `.fseventsd`, `.TemporaryItems`.
      - `__MACOSX/` archive metadata directories (left over from
        unzipping a Mac-created archive on a non-Mac).
      - Other dotfiles (`.git/*`, `.svn/*`, vim swap files, etc.) — not
        documents, never useful to ingest.
      - Files whose extension isn't in the document allowlist
        (the watcher path historically had no such check; the legacy
        Swift implementation did).
    """
    parts = relative_path.split("/")
    if any(p == "__MACOSX" for p in parts):
        return True
    # AppleDouble shadow files at any depth.
    if any(p.startswith("._") for p in parts):
        return True
    # Any dotfile component (catches .DS_Store, .git/, .svn/, .Trashes/, etc.)
    # at any depth.
    if any(p.startswith(".") and p not in ("", ".", "..") for p in parts):
        return True
    # Extension allowlist (lowercase comparison).
    suffix = Path(relative_path).suffix.lower()
    return suffix not in _ALLOWED_EXTENSIONS


def _sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    if _should_ignore(event.relative_path):
        logger.debug("watcher: ignored event for %s", event.relative_path)
        return

    existing = (
        session.query(WatchedFile)
        .filter_by(folder_id=event.folder_id, relative_path=event.relative_path)
        .one_or_none()
    )

    # Delete events
    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(UTC)
        return

    # Skip 0-byte files. Network shares (NFS / SMB / .AppleDouble-bearing
    # filesystems) frequently expose files as 0 bytes mid-copy. Hashing
    # them returns the empty-file sha256 (e3b0c4...), which is identical
    # for every empty file. The next watchdog event (after the file finishes
    # copying) will arrive with real content and be handled normally.
    try:
        if os.path.getsize(event.absolute_path) == 0:
            logger.debug("watcher: skipping 0-byte file %s", event.relative_path)
            return
    except OSError:
        # File vanished between event and stat (renamed away mid-flight, etc.)
        return

    # Create / modified events: compute sha then dispatch
    sha = _sha256_of(event.absolute_path)

    # Defensive: even if the size check above somehow lets one through
    # (race between getsize and read; truncate-to-zero between syscalls),
    # never accept the empty-file sha as a real fingerprint. Same harm
    # as the size check prevents.
    if sha == _EMPTY_FILE_SHA256:
        logger.debug("watcher: skipping event with empty-file sha for %s", event.relative_path)
        return

    # Branch 1: existing+active+same → no-op
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 == sha:
        return

    # Branch 2: existing+active+different → reprocess in place
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        return

    # Branch 3: existing+removed+same → resurrect, no re-ingest
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 == sha:
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        return

    # Branch 4: existing+removed+different → resurrect + reprocess
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        return

    # No existing row → create Document + WatchedFile + extract job
    _create_doc_and_enqueue(session, event, sha)


def _reprocess_doc(session: Session, doc_id: uuid.UUID, sha: bytes, source_path: str) -> None:
    """Bump pipeline_seq, DELETE child rows, set pipeline_status=queued, enqueue extract.

    The doc_id is preserved across content changes — chat conversations and
    citations referencing this doc keep resolving to the (now updated) doc.
    """
    doc = session.query(Document).filter_by(doc_id=doc_id).one()
    doc.pipeline_seq = doc.pipeline_seq + 1
    doc.sha256 = sha
    doc.source_path = source_path
    doc.pipeline_status = PipelineStatus.queued
    doc.error = None
    # Delete child rows explicitly (FKs have CASCADE but we do it here for
    # clarity and to avoid relying on DB-level CASCADE for application state).
    session.query(Chunk).filter_by(doc_id=doc_id).delete()
    session.query(Entity).filter_by(doc_id=doc_id).delete()
    session.query(DocumentPage).filter_by(doc_id=doc_id).delete()
    session.query(DocumentHeading).filter_by(doc_id=doc_id).delete()
    session.query(IngestionJob).filter_by(doc_id=doc_id).delete()
    session.add(IngestionJob(doc_id=doc_id, stage=JobStage.extract, status=JobStatus.queued))


def _create_doc_and_enqueue(session: Session, event: FileEvent, sha: bytes) -> None:
    filename = Path(event.absolute_path).name
    doc = Document(
        title=Path(event.absolute_path).stem,
        canonical_filename=filename,
        status="active",
        sha256=sha,
        source_path=event.absolute_path,
        pipeline_status=PipelineStatus.queued,
    )
    session.add(doc)
    session.flush()
    session.add(
        WatchedFile(
            folder_id=event.folder_id,
            relative_path=event.relative_path,
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
