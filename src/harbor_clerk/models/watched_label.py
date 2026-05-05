"""WatchedLabel model — one row per (mail_account, IMAP label) pair."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class WatchedLabel(Base):
    __tablename__ = "watched_labels"
    __table_args__ = (UniqueConstraint("account_id", "label_path", name="uq_watched_labels_account_path"),)

    label_id: Mapped[uuid_pk]
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    label_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uid_seen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]

    account: Mapped["MailAccount"] = relationship(back_populates="labels")  # type: ignore[name-defined]  # noqa: F821
    messages: Mapped[list["WatchedMessage"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="label",
        cascade="all, delete-orphan",
    )
