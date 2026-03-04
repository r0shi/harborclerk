import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    session_id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_confirm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    uploaded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    confirmed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
