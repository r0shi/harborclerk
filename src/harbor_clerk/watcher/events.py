"""Filesystem-event → database-write translation.

Pure module: no watchdog imports, no I/O scheduling. Caller passes in
synthetic FileEvent records; this module does the database work in the
provided session. Caller is responsible for commit.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.api.routes.uploads import ALLOWED_EXTENSIONS
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

logger = logging.getLogger(__name__)


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
    return suffix not in ALLOWED_EXTENSIONS


def _sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def _new_version_on_doc(session: Session, doc_id: uuid.UUID, sha: bytes, source_path: str) -> DocumentVersion:
    """Create a new DocumentVersion bound to an existing Document and update latest_version_id."""
    version = DocumentVersion(
        doc_id=doc_id,
        original_sha256=sha,
        source_path=source_path,
        status=VersionStatus.queued,
    )
    session.add(version)
    session.flush()
    doc = session.query(Document).filter_by(doc_id=doc_id).one()
    doc.latest_version_id = version.version_id
    return version


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    if _should_ignore(event.relative_path):
        logger.debug("watcher: ignored event for %s", event.relative_path)
        return

    existing = (
        session.query(WatchedFile).filter_by(folder_id=event.folder_id, relative_path=event.relative_path).one_or_none()
    )

    # Delete events
    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(UTC)
        return

    # Create / modified events: compute sha then dispatch
    sha = _sha256_of(event.absolute_path)

    # Existing active row, same sha → no-op
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 == sha:
        return

    # Existing active row, different sha → new version on the same Document
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 != sha:
        version = _new_version_on_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.version_id = version.version_id
        session.add(IngestionJob(version_id=version.version_id, stage=JobStage.extract, status=JobStatus.queued))
        return

    # Existing removed row, same sha → resurrect in place, no new Version, no job
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 == sha:
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        return

    # Existing removed row, different sha → resurrect + new version on the same Document
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 != sha:
        version = _new_version_on_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.version_id = version.version_id
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        session.add(IngestionJob(version_id=version.version_id, stage=JobStage.extract, status=JobStatus.queued))
        return

    # No existing row → check for cross-folder dedup by sha
    dedup_version = (
        session.query(DocumentVersion)
        .filter_by(original_sha256=sha)
        .order_by(DocumentVersion.created_at.desc())
        .first()
    )
    if dedup_version is not None:
        # Link to the existing doc; do not create a new Document/Version, do not enqueue a job
        session.add(
            WatchedFile(
                folder_id=event.folder_id,
                relative_path=event.relative_path,
                bookmark_data=b"",
                sha256=sha,
                doc_id=dedup_version.doc_id,
                version_id=dedup_version.version_id,
                status=WatchedFileStatus.active,
            )
        )
        return

    # Truly new content → create Document + Version + WatchedFile + extract job
    filename = Path(event.absolute_path).name
    doc = Document(title=Path(event.absolute_path).stem, canonical_filename=filename, status="active")
    session.add(doc)
    session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=sha,
        source_path=event.absolute_path,
        status=VersionStatus.queued,
    )
    session.add(version)
    session.flush()
    doc.latest_version_id = version.version_id

    session.add(
        WatchedFile(
            folder_id=event.folder_id,
            relative_path=event.relative_path,
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status=WatchedFileStatus.active,
        )
    )
    session.add(IngestionJob(version_id=version.version_id, stage=JobStage.extract, status=JobStatus.queued))
