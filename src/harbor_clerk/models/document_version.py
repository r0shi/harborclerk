import uuid
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, uuid_pk, created_at, updated_at
from harbor_clerk.models.enums import VersionStatus


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    version_id: Mapped[uuid_pk]
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
    )
    original_sha256: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        unique=True,
    )
    original_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    original_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status", create_type=False),
        nullable=False,
        server_default="queued",
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    has_text_layer: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    needs_ocr: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    extracted_chars: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        server_default=text("0"),
        nullable=True,
    )
    source_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    document = relationship("Document", back_populates="versions")
    pages = relationship(
        "DocumentPage",
        back_populates="version",
    )
