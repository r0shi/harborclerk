"""Extract stage — pull text from documents, spreadsheets, and images via Tika."""

import logging
import os
import re
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.file_types import MARKDOWN_EXTENSIONS, PLAIN_TEXT_EXTENSIONS
from harbor_clerk.models import Document, DocumentHeading, DocumentPage
from harbor_clerk.models.enums import JobStage
from harbor_clerk.storage import get_storage
from harbor_clerk.worker.heading_parser import parse_headings_from_xhtml
from harbor_clerk.worker.markdown_extract import MarkdownExtractResult, extract_markdown
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

# MIME types that are images (OCR-only, no text extraction)
IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff"}

# Filename suffixes extracted as plain UTF-8 text. A tuple because str.endswith
# requires one; sorted for deterministic ordering.
_PLAIN_TEXT_SUFFIXES = tuple(sorted(PLAIN_TEXT_EXTENSIONS))

# Filename suffixes routed through the Markdown extraction pipeline.
_MARKDOWN_SUFFIXES = tuple(sorted(MARKDOWN_EXTENSIONS))


def is_plain_text_source(mime: str, obj_key: str) -> bool:
    """True if the file should be extracted as plain UTF-8 text (no Tika).

    Single source of truth for three decisions in ``run_extract``: the
    extraction dispatch, skipping Tika heading extraction, and skipping OCR.
    """
    return mime == "text/plain" or obj_key.endswith(_PLAIN_TEXT_SUFFIXES)


# Strip ANSI escape sequences and control characters (except tab/newline) from
# untrusted strings before they land in logs or the DB error column. Java
# exceptions surfaced by Tika derive from file content and could in principle
# contain ANSI CSI sequences or carriage returns crafted by a hostile upload.
_CONTROL_CHARS_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_external_string(s: str, max_chars: int = 300) -> str:
    """Strip ANSI/control chars and collapse whitespace; cap length.

    Used for any string that originates from external content (Tika exception
    messages, file metadata, etc.) before logging or persisting it.
    """
    if not s:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", s)
    # Collapse internal whitespace runs (incl. embedded newlines) to a single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


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
    """Extract text via Apache Tika. For PDFs, splits on form feed characters.

    On 422 (Unprocessable Entity), Tika has refused to parse the file but doesn't
    say why in the response body. Refetch via `/rmeta/text` to surface the
    underlying parser exception (POI/PDFBox bug, malformed file, etc.) in the
    raised error so it shows up in the doc's pipeline_status=error message.
    """
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
    if resp.status_code == 422:
        detail = _fetch_tika_exception_detail(data, mime_type)
        raise RuntimeError(f"Tika rejected file (422): {detail}")
    resp.raise_for_status()
    text = resp.text.strip()

    if is_pdf and "\f" in text:
        # Tika/PDFBox inserts form feed (\f) between pages
        raw_pages = text.split("\f")
        return [(i + 1, p.strip()) for i, p in enumerate(raw_pages) if p.strip()]

    return _paginate_text(text, settings.synthetic_page_chars)


def _fetch_tika_exception_detail(data: bytes, mime_type: str) -> str:
    """Refetch via /rmeta/text to extract X-TIKA:EXCEPTION:container_exception.

    Best-effort diagnostic — failures here just produce a generic message.
    Truncates the stacktrace to the first line so the error column stays usable.

    The returned string passes through ``_sanitize_external_string`` so it's
    safe to log or write to the DB ``error`` column even if Tika echoes back
    crafted bytes from a hostile file (ANSI escapes, control chars, etc.).
    """
    settings = get_settings()
    try:
        resp = httpx.put(
            f"{settings.tika_url}/rmeta/text",
            content=data,
            headers={"Content-Type": mime_type, "Accept": "application/json"},
            timeout=60,
        )
        if resp.status_code != 200:
            return f"rmeta returned {resp.status_code}"
        meta_list = resp.json()
        if not isinstance(meta_list, list) or not meta_list:
            return "rmeta returned empty list"
        meta = meta_list[0]
        # Tika exposes container parser failures under this key. The value is
        # *typically* a string (exception class + message + Java stacktrace) but
        # rare exception classes can serialise as arrays or other JSON shapes —
        # treat anything non-string as missing.
        exc = meta.get("X-TIKA:EXCEPTION:container_exception", "")
        if not isinstance(exc, str) or not exc:
            return "no container_exception in rmeta response"
        # First line carries the exception type + message; rest is a Java stacktrace
        first_line = exc.split("\n", 1)[0]
        return _sanitize_external_string(first_line)
    except Exception as e:  # noqa: BLE001 — best-effort diagnostic
        return _sanitize_external_string(f"rmeta refetch failed: {type(e).__name__}: {e}")


def _alpha_ratio(text: str) -> float:
    """Fraction of alphabetic characters in text."""
    if not text:
        return 0.0
    alpha = sum(1 for c in text if c.isalpha())
    return alpha / len(text)


# MIME types where heading extraction from Tika XHTML makes no sense
_SKIP_HEADINGS_MIMES = IMAGE_MIMES | {"text/plain", "text/csv", "text/markdown"}
# Partial/legacy extension list. For plain-text formats the authoritative gate
# is is_plain_text_source() — see the skip_headings / is_never_ocr expressions.
_SKIP_HEADINGS_EXTS = (".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _extract_headings_via_tika(
    data: bytes,
    mime_type: str,
    pages: list[tuple[int, str]],
) -> list[dict]:
    """Fetch Tika XHTML and parse headings. Non-fatal — returns [] on failure.

    On 422 from the XHTML endpoint, log the actual ``container_exception`` (same
    diagnostic as the primary extract path) so the operator sees a real
    parser-error message instead of a generic "Heading extraction failed" line.
    """
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
        if resp.status_code == 422:
            detail = _fetch_tika_exception_detail(data, mime_type)
            logger.warning(
                "Heading extraction: Tika rejected XHTML for mime=%s (422): %s; continuing without headings",
                mime_type,
                detail,
            )
            return []
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


def run_extract(doc_id: uuid.UUID) -> None:
    """Download file from storage, extract text, store pages."""
    if not mark_stage_running(doc_id, JobStage.extract):
        return

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        worker_seq = doc.pipeline_seq

        # Read from storage if available; fall back to source_path for watched folder docs
        if doc.original_object_key:
            storage = get_storage()
            response = storage.get_object(doc.original_bucket, doc.original_object_key)
            data = response.read()
        elif doc.source_path and os.path.exists(doc.source_path):
            data = Path(doc.source_path).read_bytes()
        else:
            raise RuntimeError(f"No source for doc {doc_id}: source_path and original_object_key both empty")

        mime = (doc.mime_type or "").lower()
        obj_key = (doc.original_object_key or doc.source_path or "").lower()

        # Sniff RTF content regardless of extension/MIME
        is_rtf = data[:5] == b"{\\rtf"
        is_pdf = mime == "application/pdf" or obj_key.endswith(".pdf")
        is_image = mime in IMAGE_MIMES

        # Dispatch by type
        # Extension-based image detection (covers .png, .tif, .tiff not in MIME)
        if not is_image and obj_key.endswith((".png", ".tif", ".tiff")):
            is_image = True

        # Sentinel that downstream code (heading-writing) checks to decide
        # whether to use Markdown-derived headings or fall back to Tika XHTML.
        markdown_result: MarkdownExtractResult | None = None

        if is_image:
            # Image — create empty page, OCR will fill it
            pages = [(1, "")]
        elif is_plain_text_source(mime, obj_key):
            if obj_key.endswith(_MARKDOWN_SUFFIXES):
                # Markdown — frontmatter + headings + normalization
                markdown_result = extract_markdown(data)
                pages = markdown_result.pages
                if markdown_result.title:
                    doc.title = markdown_result.title
            else:
                # Other plain-text formats — direct UTF-8 decode
                pages = _extract_txt(data)
        elif is_rtf or mime == "text/rtf" or obj_key.endswith(".rtf"):
            pages = _extract_via_tika(data, "text/rtf")
        elif is_pdf:
            pages = _extract_via_tika(data, "application/pdf", is_pdf=True)
        elif mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ) or obj_key.endswith(".docx"):
            pages = _extract_via_tika(
                data,
                mime or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        else:
            # Unknown type — try Tika
            pages = _extract_via_tika(data, mime or "application/octet-stream")

        # Compute totals before the race check
        total_chars = sum(len(text) for _, text in pages)

        # Race check before writing results
        if not check_pipeline_seq(session, doc_id, worker_seq):
            logger.info("extract: pipeline_seq bumped during processing for %s, aborting write", doc_id)
            return

        # Delete existing pages for this doc (idempotency)
        existing_pages = session.execute(select(DocumentPage).where(DocumentPage.doc_id == doc_id)).scalars().all()
        for p in existing_pages:
            session.delete(p)
        session.flush()

        # Store pages
        for page_num, text in pages:
            page = DocumentPage(
                doc_id=doc_id,
                page_num=page_num,
                page_text=text,
                ocr_used=False,
            )
            session.add(page)

        # Heading source: Markdown's own parser when we ran the Markdown
        # extractor, otherwise Tika XHTML (skipped entirely for non-Markdown
        # plain text via the same predicate used for dispatch).
        if markdown_result is not None:
            headings = markdown_result.headings
        else:
            skip_headings = (
                is_image
                or mime in _SKIP_HEADINGS_MIMES
                or obj_key.endswith(_SKIP_HEADINGS_EXTS)
                or is_plain_text_source(mime, obj_key)
            )
            headings = (
                [] if skip_headings else _extract_headings_via_tika(data, mime or "application/octet-stream", pages)
            )

        # Delete existing headings (idempotency)
        existing_headings = (
            session.execute(select(DocumentHeading).where(DocumentHeading.doc_id == doc_id)).scalars().all()
        )
        for h in existing_headings:
            session.delete(h)
        session.flush()

        # Write headings (unified for Markdown and Tika sources).
        for hd in headings:
            session.add(
                DocumentHeading(
                    doc_id=doc_id,
                    level=hd["level"],
                    title=hd["title"],
                    page_num=hd["page_num"],
                    position=hd["position"],
                )
            )
        if headings:
            logger.info("Extracted %d headings for doc %s", len(headings), doc_id)

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
        # Partial/legacy extension list. For plain-text formats the authoritative gate
        # is is_plain_text_source() — see the skip_headings / is_never_ocr expressions.
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
        is_never_ocr = (
            is_rtf
            or mime in _NEVER_OCR_MIMES
            or obj_key.endswith(_NEVER_OCR_EXTS)
            or is_plain_text_source(mime, obj_key)
        )

        if is_image:
            doc.needs_ocr = True
            doc.has_text_layer = False
        elif is_pdf:
            all_text = " ".join(text for _, text in pages)
            ratio = _alpha_ratio(all_text)
            doc.has_text_layer = total_chars > 0
            doc.needs_ocr = total_chars < 500 or ratio < 0.2
        elif is_never_ocr:
            doc.needs_ocr = False
            doc.has_text_layer = True
        else:
            # Unknown type — don't OCR
            doc.needs_ocr = False
            doc.has_text_layer = total_chars > 0

        doc.extracted_chars = total_chars

        session.commit()
        logger.info(
            "Extracted %d pages, %d chars for doc %s (needs_ocr=%s)",
            len(pages),
            total_chars,
            doc_id,
            doc.needs_ocr,
        )
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.extract, worker_seq=worker_seq)
