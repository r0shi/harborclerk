from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, uuid_pk


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[uuid_pk]
    client_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False)
    grant_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    response_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="mcp",
    )
    client_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at]
