import uuid
from typing import Optional

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
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pgvector.sqlalchemy import Vector

from harbor_clerk.models.base import Base, uuid_pk, created_at


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[uuid_pk]
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_num: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="english",
    )
    ocr_used: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Dual tsvector columns for bilingual FTS (read-only, generated)
    fts_en: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(chunk_text, ''))", persisted=True),
    )
    fts_fr: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('french', coalesce(chunk_text, ''))", persisted=True),
    )

    # pgvector embedding
    embedding = mapped_column(Vector(384), nullable=True)

    created_at: Mapped[created_at]

    entities = relationship(
        "Entity", back_populates="chunk", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("version_id", "chunk_num", name="uq_chunks_version_num"),
    )
