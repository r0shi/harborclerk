import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk, created_at


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[uuid_pk]
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tool_calls: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rag_context: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[created_at]

    __table_args__ = (Index("idx_messages_conv", "conversation_id", "created_at"),)
