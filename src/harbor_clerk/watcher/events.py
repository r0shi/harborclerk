"""Filesystem-event → database-write translation.

Pure module: no watchdog imports, no I/O scheduling. Caller passes in
synthetic FileEvent records; this module does the database work in the
provided session. Caller is responsible for commit.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus


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


def _sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    existing = (
        session.query(WatchedFile).filter_by(folder_id=event.folder_id, relative_path=event.relative_path).one_or_none()
    )

    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(UTC)
        return

    sha = _sha256_of(event.absolute_path)

    if existing is not None and existing.sha256 == sha and existing.status == WatchedFileStatus.active:
        return  # no-op

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

    if existing is None:
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
    else:
        existing.sha256 = sha
        existing.doc_id = doc.doc_id
        existing.version_id = version.version_id
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        existing.bookmark_data = b""

    session.add(IngestionJob(version_id=version.version_id, stage=JobStage.extract, status=JobStatus.queued))
