import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_id: Mapped[uuid_pk]
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_clients.client_id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="S256",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    created_at: Mapped[created_at]
