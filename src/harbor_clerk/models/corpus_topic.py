import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class CorpusTopic(Base):
    __tablename__ = "corpus_topics"

    topic_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    representative_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorpusTopicsMeta(Base):
    __tablename__ = "corpus_topics_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    last_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    corpus_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
