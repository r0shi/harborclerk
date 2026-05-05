"""WatchedMessage model — per-message tracking, analog of WatchedFile."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class WatchedMessage(Base):
    __tablename__ = "watched_messages"
    __table_args__ = (UniqueConstraint("label_id", "message_id", name="uq_watched_messages_label_message"),)

    message_pk: Mapped[uuid_pk]
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("watched_labels.label_id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    imap_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eml_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    email_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # Reuses the `created_at` TypeAlias from base.py — same DateTime+now()+nullable=False
    # shape, just under a different column name. The column name comes from the attribute.
    first_seen_at: Mapped[created_at]
    unlabeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    label: Mapped["WatchedLabel"] = relationship(back_populates="messages")  # type: ignore[name-defined]  # noqa: F821
