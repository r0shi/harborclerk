"""Extract stage — pull text from documents, spreadsheets, and images via Tika."""

import logging
import uuid

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import DocumentHeading, DocumentPage, DocumentVersion
from harbor_clerk.models.enums import JobStage
from harbor_clerk.storage import get_storage
from harbor_clerk.worker.heading_parser import parse_headings_from_xhtml
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

# MIME types that are images (OCR-only, no text extraction)
IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff"}


def _paginate_text(text: str, target: int) -> list[tuple[int, str]]:
    """Split a long text into synthetic pages at paragraph boundaries.

    Returns [(page_num, text)] with 1-based page numbers.
    """
    if not text or target <= 0:
        return [(1, text)]

    if len(text) <= target:
        return [(1, text)]

    pages: list[tuple[int, str]] = []
    start = 0
    page_num = 1
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)

        if end < text_len:
            # Try to break at a paragraph boundary (double newline)
            para = text.rfind("\n\n", start, end)
            if para > start + target // 2:
                end = para + 2  # include the double newline
            else:
                # Fall back to single newline
                nl = text.rfind("\n", start, end)
                if nl > start + target // 2:
                    end = nl + 1

        pages.append((page_num, text[start:end]))
        page_num += 1
        start = end

    return pages


def _extract_txt(data: bytes) -> list[tuple[int, str]]:
    """Plain text, split into synthetic pages."""
    settings = get_settings()
    text = data.decode("utf-8", errors="replace")
    return _paginate_text(text, settings.synthetic_page_chars)


def _extract_via_tika(data: bytes, mime_type: str, is_pdf: bool = False) -> list[tuple[int, str]]:
    """Extract text via Apache Tika. For PDFs, splits on form feed characters."""
    settings = get_settings()
    if not settings.tika_url:
        raise RuntimeError(
            "Tika is required for extraction (TIKA_URL not set). Only plain text and images work without Tika."
        )
    resp = httpx.put(
        f"{settings.tika_url}/tika",
        content=data,
        headers={"Content-Type": mime_type, "Accept": "text/plain"},
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.text.strip()

    if is_pdf and "\f" in text:
        # Tika/PDFBox inserts form feed (\f) between pages
        raw_pages = text.split("\f")
        return [(i + 1, p.strip()) for i, p in enumerate(raw_pages) if p.strip()]

    return _paginate_text(text, settings.synthetic_page_chars)


def _alpha_ratio(text: str) -> float:
    """Fraction of alphabetic characters in text."""
    if not text:
        return 0.0
    alpha = sum(1 for c in text if c.isalpha())
    return alpha / len(text)


# MIME types where heading extraction from Tika XHTML makes no sense
_SKIP_HEADINGS_MIMES = IMAGE_MIMES | {"text/plain", "text/csv", "text/markdown"}
_SKIP_HEADINGS_EXTS = (".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _extract_headings_via_tika(
    data: bytes,
    mime_type: str,
    pages: list[tuple[int, str]],
) -> list[dict]:
    """Fetch Tika XHTML and parse headings. Non-fatal — returns [] on failure."""
    settings = get_settings()
    if not settings.tika_url:
        return []
    try:
        resp = httpx.put(
            f"{settings.tika_url}/tika",
            content=data,
            headers={"Content-Type": mime_type, "Accept": "text/html"},
            timeout=120,
        )
        resp.raise_for_status()
        raw_headings = parse_headings_from_xhtml(resp.text)
        if not raw_headings:
            return []

        # Build cumulative char offsets per page for position→page mapping.
        # NOTE: Heading positions come from XHTML text nodes, page offsets
        # come from plain-text output. This is a heuristic mapping and may
        # be off near page boundaries.
        cum_offsets: list[tuple[int, int, int]] = []  # (start, end, page_num)
        offset = 0
        for page_num, text in pages:
            end = offset + len(text)
            cum_offsets.append((offset, end, page_num))
            offset = end

        result = []
        for h in raw_headings:
            page_num = None
            for _start, end, pnum in cum_offsets:
                if h.position < end:
                    page_num = pnum
                    break
            result.append(
                {
                    "level": h.level,
                    "title": h.title,
                    "page_num": page_num,
                    "position": h.position,
                }
            )
        return result
    except Exception:
        logger.warning(
            "Heading extraction failed for mime=%s, continuing without headings",
            mime_type,
            exc_info=True,
        )
        return []


def run_extract(version_id: uuid.UUID) -> None:
    """Download file from storage, extract text, store pages."""
    if not mark_stage_running(version_id, JobStage.extract):
        return

    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        # Download from storage
        storage = get_storage()
        response = storage.get_object(version.original_bucket, version.original_object_key)
        data = response.read()

        mime = (version.mime_type or "").lower()

        # Sniff RTF content regardless of extension/MIME
        is_rtf = data[:5] == b"{\\rtf"
        is_pdf = mime == "application/pdf" or version.original_object_key.endswith(".pdf")
        is_image = mime in IMAGE_MIMES

        # Dispatch by type
        # Extension-based image detection (covers .png, .tif, .tiff not in MIME)
        obj_key = version.original_object_key.lower()
        if not is_image and obj_key.endswith((".png", ".tif", ".tiff")):
            is_image = True

        if is_image:
            # Image — create empty page, OCR will fill it
            pages = [(1, "")]
        elif mime == "text/plain" or obj_key.endswith((".txt", ".md", ".csv")):
            # Plain text / Markdown / CSV — no Tika needed
            pages = _extract_txt(data)
        elif is_rtf or mime == "text/rtf" or version.original_object_key.endswith(".rtf"):
            pages = _extract_via_tika(data, "text/rtf")
        elif is_pdf:
            pages = _extract_via_tika(data, "application/pdf", is_pdf=True)
        elif mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ) or version.original_object_key.endswith(".docx"):
            pages = _extract_via_tika(
                data,
                mime or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        else:
            # Unknown type — try Tika
            pages = _extract_via_tika(data, mime or "application/octet-stream")

        # Delete existing pages for this version (idempotency)
        existing_pages = (
            session.execute(select(DocumentPage).where(DocumentPage.version_id == version_id)).scalars().all()
        )
        for p in existing_pages:
            session.delete(p)
        session.flush()

        # Store pages
        total_chars = 0
        for page_num, text in pages:
            page = DocumentPage(
                version_id=version_id,
                page_num=page_num,
                page_text=text,
                ocr_used=False,
            )
            session.add(page)
            total_chars += len(text)

        # Extract headings from Tika XHTML (skip images and plain text)
        skip_headings = is_image or mime in _SKIP_HEADINGS_MIMES or obj_key.endswith(_SKIP_HEADINGS_EXTS)
        # Delete existing headings (idempotency)
        existing_headings = (
            session.execute(select(DocumentHeading).where(DocumentHeading.version_id == version_id)).scalars().all()
        )
        for h in existing_headings:
            session.delete(h)
        session.flush()

        if not skip_headings:
            headings = _extract_headings_via_tika(data, mime or "application/octet-stream", pages)
            for hd in headings:
                session.add(
                    DocumentHeading(
                        version_id=version_id,
                        level=hd["level"],
                        title=hd["title"],
                        page_num=hd["page_num"],
                        position=hd["position"],
                    )
                )
            if headings:
                logger.info(
                    "Extracted %d headings for version %s",
                    len(headings),
                    version_id,
                )

        # Determine if OCR is needed
        _NEVER_OCR_MIMES = {
            "text/plain",
            "text/rtf",
            "text/html",
            "text/csv",
            "text/markdown",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-excel",
            "application/vnd.ms-powerpoint",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "application/epub+zip",
            "message/rfc822",
        }
        _NEVER_OCR_EXTS = (
            ".docx",
            ".doc",
            ".txt",
            ".rtf",
            ".md",
            ".csv",
            ".odt",
            ".pages",
            ".xlsx",
            ".xls",
            ".ods",
            ".numbers",
            ".pptx",
            ".ppt",
            ".odp",
            ".key",
            ".epub",
            ".html",
            ".htm",
            ".eml",
        )
        is_never_ocr = is_rtf or mime in _NEVER_OCR_MIMES or obj_key.endswith(_NEVER_OCR_EXTS)

        if is_image:
            version.needs_ocr = True
            version.has_text_layer = False
        elif is_pdf:
            all_text = " ".join(text for _, text in pages)
            ratio = _alpha_ratio(all_text)
            version.has_text_layer = total_chars > 0
            version.needs_ocr = total_chars < 500 or ratio < 0.2
        elif is_never_ocr:
            version.needs_ocr = False
            version.has_text_layer = True
        else:
            # Unknown type — don't OCR
            version.needs_ocr = False
            version.has_text_layer = total_chars > 0

        version.extracted_chars = total_chars

        session.commit()
        logger.info(
            "Extracted %d pages, %d chars for version %s (needs_ocr=%s)",
            len(pages),
            total_chars,
            version_id,
            version.needs_ocr,
        )
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.extract)
