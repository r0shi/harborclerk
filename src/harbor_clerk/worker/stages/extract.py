"""Extract stage — pull text from PDF/DOCX/RTF/TXT/JPEG."""

import io
import logging
import uuid

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.storage import get_storage
from harbor_clerk.models import DocumentPage, DocumentVersion
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def _extract_pdf(data: bytes) -> list[tuple[int, str]]:
    """Extract text from PDF using PyMuPDF. Returns [(page_num, text)]."""
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            pages.append((i + 1, text))
    return pages


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


def _extract_docx(data: bytes) -> list[tuple[int, str]]:
    """Extract text from DOCX, split into synthetic pages."""
    from docx import Document as DocxDocument

    settings = get_settings()
    doc = DocxDocument(io.BytesIO(data))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    return _paginate_text(full_text, settings.synthetic_page_chars)


def _extract_txt(data: bytes) -> list[tuple[int, str]]:
    """Plain text, split into synthetic pages."""
    settings = get_settings()
    text = data.decode("utf-8", errors="replace")
    return _paginate_text(text, settings.synthetic_page_chars)


def _extract_rtf(data: bytes) -> list[tuple[int, str]]:
    """Extract text from RTF using striprtf (no Tika needed)."""
    from striprtf.striprtf import rtf_to_text

    settings = get_settings()
    raw = data.decode("utf-8", errors="replace")
    text = rtf_to_text(raw)
    return _paginate_text(text.strip(), settings.synthetic_page_chars)


def _extract_via_tika(data: bytes, mime_type: str) -> list[tuple[int, str]]:
    """Fallback extraction via Apache Tika, split into synthetic pages."""
    settings = get_settings()
    if not settings.tika_url:
        raise RuntimeError("Tika is not configured (TIKA_URL is empty)")
    resp = httpx.put(
        f"{settings.tika_url}/tika",
        content=data,
        headers={"Content-Type": mime_type, "Accept": "text/plain"},
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.text.strip()
    return _paginate_text(text, settings.synthetic_page_chars)


def _alpha_ratio(text: str) -> float:
    """Fraction of alphabetic characters in text."""
    if not text:
        return 0.0
    alpha = sum(1 for c in text if c.isalpha())
    return alpha / len(text)


def run_extract(version_id: uuid.UUID) -> None:
    """Download file from MinIO, extract text, store pages."""
    if not mark_stage_running(version_id, JobStage.extract):
        return

    session = get_sync_session()
    try:
        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one()

        # Download from storage
        storage = get_storage()
        response = storage.get_object(version.original_bucket, version.original_object_key)
        data = response.read()

        mime = (version.mime_type or "").lower()

        # Sniff RTF content regardless of extension/MIME — files are often
        # mislabeled (e.g. .txt containing RTF markup)
        is_rtf = data[:5] == b"{\\rtf"

        settings = get_settings()

        # Dispatch by mime type (with content sniffing overrides)
        if is_rtf or mime == "text/rtf" or version.original_object_key.endswith(".rtf"):
            if settings.tika_url:
                pages = _extract_via_tika(data, "text/rtf")
            else:
                pages = _extract_rtf(data)
        elif mime == "application/pdf" or version.original_object_key.endswith(".pdf"):
            pages = _extract_pdf(data)
        elif mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ) or version.original_object_key.endswith(".docx"):
            pages = _extract_docx(data)
        elif mime == "text/plain" or version.original_object_key.endswith(".txt"):
            pages = _extract_txt(data)
        elif mime in ("image/jpeg", "image/png", "image/tiff"):
            # Image — create empty page, OCR will fill it
            pages = [(1, "")]
        elif settings.tika_url:
            # Fallback to Tika
            pages = _extract_via_tika(data, mime or "application/octet-stream")
        else:
            # No Tika — try plain text as last resort
            pages = _extract_txt(data)

        # Delete existing pages for this version (idempotency)
        existing_pages = session.execute(
            select(DocumentPage).where(DocumentPage.version_id == version_id)
        ).scalars().all()
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

        # Determine if OCR is needed
        is_image = mime in ("image/jpeg", "image/png", "image/tiff")
        is_pdf = mime == "application/pdf" or version.original_object_key.endswith(".pdf")
        is_never_ocr = is_rtf or mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain", "text/rtf",
        ) or version.original_object_key.endswith((".docx", ".txt", ".rtf"))

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
            len(pages), total_chars, version_id, version.needs_ocr,
        )
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.extract)
