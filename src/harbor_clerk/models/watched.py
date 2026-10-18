import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class WatchedFileStatus(PyEnum):
    active = "active"
    removed = "removed"


# `watched_folders.unavailable_reason` values. Docker discovery sets UNMOUNTED
# (and disables the folder) when a mount under WATCH_ROOT goes away. The
# watcher sets the ROOT_* values when a reconcile could not compare the folder;
# they are informational: the folder's documents stay searchable, so it can
# still be scoped, picked and protected from deletion. Mirrored in
# frontend/src/utils/watchedFolders.ts.
UNMOUNTED = "unmounted"
ROOT_MISSING = "root_missing"
ROOT_UNREADABLE = "root_unreadable"
ROOT_EMPTY = "root_empty"
WATCHER_UNAVAILABLE_REASONS = (ROOT_MISSING, ROOT_UNREADABLE, ROOT_EMPTY)


def folder_is_unavailable(reason: str | None) -> bool:
    """Whether a folder with this ``unavailable_reason`` is unavailable for use (scoping, picking).

    Only an unmounted Docker mount is; the watcher's reasons describe a pass
    it had to skip and leave the folder usable.
    """
    return reason == UNMOUNTED


class WatchedFolder(Base):
    __tablename__ = "watched_folders"

    folder_id: Mapped[uuid_pk]
    path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_discovered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Phase 5: skipped-file hygiene. Updated by the watcher's initial scan.
    # ``skipped_count`` is the number of files in the folder whose extension
    # was unsupported (dotfiles / AppleDouble / __MACOSX / Excalidraw are
    # excluded — they're noise, not "files the user might expect ingested").
    # ``skipped_extensions`` is the sorted distinct list of those extensions
    # (lowercase, with leading dot, e.g. ``[".canvas", ".excalidraw"]``).
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_extensions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[created_at]

    files: Mapped[list["WatchedFile"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class WatchedFile(Base):
    __tablename__ = "watched_files"

    file_id: Mapped[uuid_pk]
    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("watched_folders.folder_id", ondelete="CASCADE"),
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[WatchedFileStatus] = mapped_column(
        Enum(WatchedFileStatus, name="watched_file_status", create_type=False),
        nullable=False,
        server_default="active",
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    folder: Mapped["WatchedFolder"] = relationship(back_populates="files")
