"""Structured source references and citation formatting.

This module is intentionally small and mostly pure. The builder never queries
the database; callers that need watched-folder or parent-email context should
load it up front with ``load_source_ref_context`` and then call into the pure
builder.
"""

from __future__ import annotations

import logging
import posixpath
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from sqlalchemy import exc as sa_exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

logger = logging.getLogger(__name__)

SourceKind = Literal["document", "email", "attachment", "unknown"]


@dataclass(frozen=True)
class SourceRef:
    """A stable source/citation object for API, MCP, CLI, and UI payloads."""

    doc_id: str
    doc_title: str
    source_kind: SourceKind
    source_label: str
    citation: str
    chunk_id: str | None = None
    pages: str | None = None
    section: str | None = None
    folder_label: str | None = None
    relative_path: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-ready dict, omitting absent optional fields."""
        out = {
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "citation": self.citation,
        }
        if self.chunk_id:
            out["chunk_id"] = self.chunk_id
        if self.pages:
            out["pages"] = self.pages
        if self.section:
            out["section"] = self.section
        if self.folder_label:
            out["folder_label"] = self.folder_label
        if self.relative_path:
            out["relative_path"] = self.relative_path
        return out


@dataclass
class SourceRefContext:
    """Bulk-loaded context for building SourceRefs without N+1 queries."""

    docs_by_id: dict[uuid.UUID, Document]
    watched_files_by_doc: dict[uuid.UUID, WatchedFile]
    watched_folders_by_id: dict[uuid.UUID, WatchedFolder]
    parent_email_docs_by_id: dict[uuid.UUID, Document]

    def ref_for_doc(
        self,
        doc_or_id: Document | uuid.UUID | str,
        *,
        chunk_id: str | uuid.UUID | None = None,
        pages: str | None = None,
        section: str | None = None,
        include_relative_path: bool = True,
    ) -> SourceRef:
        """Build a SourceRef for a loaded document or document id."""
        if isinstance(doc_or_id, Document):
            doc = doc_or_id
            doc_id = doc.doc_id
        else:
            doc_id = _coerce_uuid(doc_or_id)
            doc = self.docs_by_id.get(doc_id)

        watched_file = self.watched_files_by_doc.get(doc_id)
        watched_folder = self.watched_folders_by_id.get(watched_file.folder_id) if watched_file else None
        parent_email_doc = None
        if doc is not None and doc.email_parent_doc_id is not None:
            parent_email_doc = self.parent_email_docs_by_id.get(doc.email_parent_doc_id)

        return build_source_ref(
            doc=doc,
            chunk_id=str(chunk_id) if chunk_id else None,
            pages=pages,
            section=section,
            watched_file=watched_file,
            watched_folder=watched_folder,
            parent_email_doc=parent_email_doc,
            include_relative_path=include_relative_path,
        )


async def load_source_ref_context(
    session: AsyncSession,
    doc_ids: Iterable[uuid.UUID | str],
) -> SourceRefContext:
    """Bulk-load source context for the given document ids."""
    normalized_ids = [_coerce_uuid(doc_id) for doc_id in doc_ids]
    unique_ids = list(dict.fromkeys(normalized_ids))
    if not unique_ids:
        return SourceRefContext({}, {}, {}, {})

    docs_result = await session.execute(select(Document).where(Document.doc_id.in_(unique_ids)))
    docs_by_id = {doc.doc_id: doc for doc in docs_result.scalars().all()}

    parent_ids = list(
        {
            doc.email_parent_doc_id
            for doc in docs_by_id.values()
            if doc.email_parent_doc_id is not None and doc.email_parent_doc_id not in docs_by_id
        }
    )
    parent_email_docs_by_id: dict[uuid.UUID, Document] = {}
    if parent_ids:
        parent_result = await session.execute(select(Document).where(Document.doc_id.in_(parent_ids)))
        parent_email_docs_by_id = {doc.doc_id: doc for doc in parent_result.scalars().all()}

    watched_files_by_doc: dict[uuid.UUID, WatchedFile] = {}
    watched_folders_by_id: dict[uuid.UUID, WatchedFolder] = {}
    try:
        wf_result = await session.execute(
            select(WatchedFile).where(
                WatchedFile.doc_id.in_(unique_ids),
                WatchedFile.status == WatchedFileStatus.active,
            )
        )
        for watched_file in wf_result.scalars().all():
            if watched_file.doc_id is not None and watched_file.doc_id not in watched_files_by_doc:
                watched_files_by_doc[watched_file.doc_id] = watched_file

        folder_ids = list({wf.folder_id for wf in watched_files_by_doc.values()})
        if folder_ids:
            folder_result = await session.execute(select(WatchedFolder).where(WatchedFolder.folder_id.in_(folder_ids)))
            watched_folders_by_id = {folder.folder_id: folder for folder in folder_result.scalars().all()}
    except (sa_exc.OperationalError, sa_exc.ProgrammingError):
        logger.debug("Failed to load watched-folder context for SourceRef", exc_info=True)

    return SourceRefContext(
        docs_by_id=docs_by_id,
        watched_files_by_doc=watched_files_by_doc,
        watched_folders_by_id=watched_folders_by_id,
        parent_email_docs_by_id=parent_email_docs_by_id,
    )


def format_pages(page_start: int | None, page_end: int | None = None) -> str | None:
    """Format a page or page range as ``"4"`` or ``"4-5"``."""
    if page_start is None:
        return None
    if page_end is None or page_end == page_start:
        return str(page_start)
    return f"{page_start}-{page_end}"


def format_document_citation(title: str, pages: str | None = None) -> str:
    """Format a document citation."""
    title = _clean(title) or "Untitled document"
    page_label = _page_label(pages)
    return f"{title}, {page_label}" if page_label else title


def format_email_citation(
    *,
    from_name: str | None = None,
    from_address: str | None = None,
    subject: str | None = None,
    date_sent: datetime | date | str | None = None,
) -> str:
    """Format an email-native citation from the available metadata."""
    sender = _display_sender(from_name=from_name, from_address=from_address)
    pieces = [f"Email from {sender}" if sender else "Email"]
    clean_subject = _clean(subject)
    if clean_subject:
        pieces.append(f'"{clean_subject}"')
    formatted_date = _format_date(date_sent)
    if formatted_date:
        pieces.append(formatted_date)
    return ", ".join(pieces)


def format_attachment_citation(
    *,
    attachment_label: str,
    pages: str | None = None,
    parent_email_doc: Document | None = None,
) -> str:
    """Format an attachment citation, including parent email context when available."""
    label = _clean(attachment_label) or "attachment"
    base = f'Attachment "{label}"'
    page_label = _page_label(pages)
    if page_label:
        base = f"{base}, {page_label}"
    if parent_email_doc is None:
        return base
    return f"{base}, to {_email_citation_for_doc(parent_email_doc)}"


def build_source_ref(
    *,
    doc: Document | None,
    chunk_id: str | None = None,
    pages: str | None = None,
    section: str | None = None,
    watched_file: WatchedFile | None = None,
    watched_folder: WatchedFolder | None = None,
    parent_email_doc: Document | None = None,
    include_relative_path: bool = True,
) -> SourceRef:
    """Build a SourceRef from preloaded document/source context."""
    doc_id = str(doc.doc_id) if doc is not None and doc.doc_id is not None else ""
    relative_path = _safe_relative_path(watched_file.relative_path if watched_file else None)
    doc_title = _document_title(doc, relative_path)
    folder_label = _folder_label(watched_folder)

    if doc is None:
        source_kind: SourceKind = "unknown"
        source_label = doc_title
        citation = format_document_citation(doc_title, pages)
    elif _is_attachment_doc(doc):
        source_kind = "attachment"
        source_label = _clean(doc.title) or _clean(doc.canonical_filename) or _leaf(relative_path) or "attachment"
        citation = format_attachment_citation(
            attachment_label=source_label,
            pages=pages,
            parent_email_doc=parent_email_doc,
        )
    elif _is_email_doc(doc, relative_path):
        source_kind = "email"
        email_fields = _email_fields_for_doc(doc)
        citation = format_email_citation(**email_fields)
        source_label = citation
        if citation == "Email":
            source_label = doc_title
            citation = format_document_citation(doc_title, pages)
    else:
        source_kind = "document"
        source_label = doc_title
        citation = format_document_citation(doc_title, pages)

    return SourceRef(
        doc_id=doc_id,
        doc_title=doc_title,
        chunk_id=chunk_id,
        pages=_clean(pages),
        section=_clean(section),
        source_kind=source_kind,
        source_label=source_label,
        folder_label=folder_label,
        relative_path=relative_path if include_relative_path else None,
        citation=citation,
    )


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _leaf(path: str | None) -> str | None:
    clean = _clean(path)
    if not clean:
        return None
    return posixpath.basename(clean)


def _document_title(doc: Document | None, relative_path: str | None) -> str:
    if doc is None:
        return _leaf(relative_path) or "Untitled document"
    return _clean(doc.title) or _clean(doc.canonical_filename) or _leaf(relative_path) or "Untitled document"


def _folder_label(folder: WatchedFolder | None) -> str | None:
    if folder is None:
        return None
    return _clean(folder.display_name) or _leaf(folder.path)


def _safe_relative_path(relative_path: str | None) -> str | None:
    clean = _clean(relative_path)
    if not clean:
        return None
    normalized = clean.replace("\\", "/")
    if posixpath.isabs(normalized):
        return posixpath.basename(normalized)
    return normalized


def _page_label(pages: str | None) -> str | None:
    clean = _clean(pages)
    if not clean:
        return None
    return f"pp. {clean}" if "-" in clean else f"p. {clean}"


def _display_sender(*, from_name: str | None, from_address: str | None) -> str | None:
    return _clean(from_name) or _clean(from_address)


def _format_date(value: datetime | date | str | None) -> str | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def _parse_date(value: datetime | date | str | None) -> datetime | date | None:
    if isinstance(value, datetime | date):
        return value
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _is_attachment_doc(doc: Document) -> bool:
    return doc.email_parent_doc_id is not None


def _is_email_doc(doc: Document, relative_path: str | None) -> bool:
    if any(
        _clean(v)
        for v in (
            doc.email_message_id,
            doc.email_from_address,
            doc.email_from_name,
            doc.email_subject,
        )
    ):
        return True
    if doc.email_date_sent is not None:
        return True
    if _clean(doc.mime_type) == "message/rfc822":
        return True
    filename = (_clean(doc.canonical_filename) or _leaf(relative_path) or "").lower()
    return filename.endswith(".eml")


def _email_fields_for_doc(doc: Document) -> dict[str, str | datetime | date | None]:
    tika = doc.doc_metadata.get("tika") if isinstance(doc.doc_metadata, dict) else None
    if not isinstance(tika, dict):
        tika = {}
    return {
        "from_name": doc.email_from_name,
        "from_address": doc.email_from_address or _clean(tika.get("email_from")),
        "subject": doc.email_subject or _clean(tika.get("email_subject")),
        "date_sent": doc.email_date_sent or _clean(tika.get("email_date")),
    }


def _email_citation_for_doc(doc: Document) -> str:
    fields = _email_fields_for_doc(doc)
    citation = format_email_citation(**fields)
    if citation != "Email":
        return citation
    return format_document_citation(_document_title(doc, None))
