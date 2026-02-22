import uuid
from typing import Optional

from sqlalchemy import BigInteger, Enum, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk, created_at
from harbor_clerk.models.enums import UploadSource


class Upload(Base):
    __tablename__ = "uploads"

    upload_id: Mapped[uuid_pk]
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[UploadSource] = mapped_column(
        Enum(UploadSource, name="upload_source", create_type=False),
        nullable=False,
        server_default="web",
    )
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    minio_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    minio_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="queued",
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
