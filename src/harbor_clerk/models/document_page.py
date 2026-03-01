import uuid

from sqlalchemy import (
    Boolean,
    Computed,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, uuid_pk


class DocumentPage(Base):
    __tablename__ = "document_pages"

    page_id: Mapped[uuid_pk]
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    page_num: Mapped[int] = mapped_column(Integer, nullable=False)
    page_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    ocr_used: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    char_count: Mapped[int | None] = mapped_column(
        Integer,
        Computed("char_length(page_text)", persisted=True),
    )

    version = relationship("DocumentVersion", back_populates="pages")

    __table_args__ = (UniqueConstraint("version_id", "page_num", name="uq_pages_version_page"),)
