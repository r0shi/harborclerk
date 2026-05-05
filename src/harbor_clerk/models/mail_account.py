"""MailAccount model — one row per IMAP connection."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    __table_args__ = (UniqueConstraint("imap_host", "imap_username", name="uq_mail_accounts_host_username"),)

    account_id: Mapped[uuid_pk]
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # gmail|icloud|fastmail|yahoo|generic
    imap_host: Mapped[str] = mapped_column(Text, nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default="993")
    imap_username: Mapped[str] = mapped_column(Text, nullable=False)
    app_password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]

    labels: Mapped[list["WatchedLabel"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="account",
        cascade="all, delete-orphan",
    )
