"""DocumentLink — a parsed wikilink from one Document to another (or to a
target that doesn't exist yet).

Populated by the extract stage from Markdown ``[[…]]`` patterns. Resolved
by the finalize stage against the active corpus.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk


class DocumentLink(Base):
    __tablename__ = "document_links"

    link_id: Mapped[uuid_pk]

    # Source — the document whose body contained the [[…]]. Outgoing links
    # for src_doc_id are deleted when the source is deleted (CASCADE).
    src_doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Target — the document the link resolved to, or NULL if still unresolved.
    # SET NULL on target delete (not CASCADE) so the graph remembers the
    # broken reference; a future doc with the same name can re-resolve it.
    target_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The raw inner text of the [[…]] match, exactly as it appeared.
    link_text: Mapped[str] = mapped_column(Text, nullable=False)

    # The parsed target name (before `#` and `|`), lowercased + stripped.
    # Indexed for re-resolution lookups when a new doc finalizes.
    target_title: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Optional heading anchor (`#Section` portion).
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional display alias (`|Alias` portion).
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True once finalize has identified a unique target. Stays True after
    # the resolver runs even if the target is later deleted (target_doc_id
    # would become NULL via the SET NULL FK).
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
