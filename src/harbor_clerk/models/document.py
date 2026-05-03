from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk
from harbor_clerk.models.enums import PipelineStatus


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[uuid_pk]
    title: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    topic_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corpus_topics.topic_id", ondelete="SET NULL"), nullable=True
    )

    # Per-content state (pulled up from former DocumentVersion in 0017 migration)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pipeline_status: Mapped[PipelineStatus] = mapped_column(
        Enum(PipelineStatus, name="pipeline_status", create_type=False),
        nullable=False,
    )
    pipeline_seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_text_layer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    needs_ocr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extracted_chars: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ISO 639-1 codes of the languages this doc was OCR'd with. NULL for
    # non-OCR'd docs and for legacy pre-language-packs docs (we can't
    # retroactively know what was used). Populated by worker/stages/ocr.py
    # after a successful OCR pass.
    ocr_languages_used: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
        nullable=True,
    )

    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
