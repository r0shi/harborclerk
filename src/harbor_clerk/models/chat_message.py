import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


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
    tool_calls: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rag_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    context_pct: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[created_at]
    # Insertion order (#809). created_at is now(), the transaction's start, and a chat turn writes all its rows in
    # one transaction, so they tie on it; the identity hands each row a strictly increasing value as it is inserted.
    # Every reader orders by (created_at, seq), never by created_at alone (tests/test_chat_message_order.py).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)

    __table_args__ = (Index("idx_messages_conv", "conversation_id", "created_at", "seq"),)
