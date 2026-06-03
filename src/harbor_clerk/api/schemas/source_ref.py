"""Shared source/citation response schemas."""

from typing import Literal

from pydantic import BaseModel


class SourceRefOut(BaseModel):
    doc_id: str
    doc_title: str
    chunk_id: str | None = None
    pages: str | None = None
    section: str | None = None
    source_kind: Literal["document", "email", "attachment", "unknown"]
    source_label: str
    folder_label: str | None = None
    relative_path: str | None = None
    citation: str
