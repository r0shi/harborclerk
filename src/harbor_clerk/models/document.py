import uuid

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[uuid_pk]
    title: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="active",
    )
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    versions = relationship(
        "DocumentVersion",
        back_populates="document",
    )
