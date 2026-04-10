from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    created_at: Mapped[created_at]
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Scoping (added in migration 0013)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    permission_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default="full")
    tool_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    scope_topic_ids: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    scope_folder_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    max_snippet_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_rph: Mapped[int | None] = mapped_column(Integer, nullable=True)
