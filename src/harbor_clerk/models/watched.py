import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class WatchedFileStatus(PyEnum):
    active = "active"
    removed = "removed"


class WatchedFolder(Base):
    __tablename__ = "watched_folders"

    folder_id: Mapped[uuid_pk]
    path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[WatchedFileStatus] = mapped_column(
        Enum(WatchedFileStatus, name="watched_file_status", create_type=False),
        nullable=False,
        server_default="active",
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    folder: Mapped["WatchedFolder"] = relationship(back_populates="files")
