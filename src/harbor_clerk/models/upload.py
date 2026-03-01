import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk
from harbor_clerk.models.enums import UploadSource


class Upload(Base):
    __tablename__ = "uploads"

    upload_id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
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
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    minio_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    minio_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="queued",
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
