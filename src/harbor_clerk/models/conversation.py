import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk, created_at, updated_at


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        server_default="New conversation",
    )
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)
