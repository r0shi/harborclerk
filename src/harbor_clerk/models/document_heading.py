import uuid

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk


class DocumentHeading(Base):
    __tablename__ = "document_headings"

    heading_id: Mapped[uuid_pk]
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    page_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
