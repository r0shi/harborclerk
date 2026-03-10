from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class ModelSettings(Base):
    __tablename__ = "model_settings"

    model_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    settings: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="{}")
