"""Chunk stage — split page text into overlapping chunks."""

import logging
import re
import uuid

from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, DocumentPage, DocumentVersion
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

# Sentence boundary pattern
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_text(text: str, target: int = 1000, overlap: int = 150) -> list[tuple[int, int]]:
    """Return list of (char_start, char_end) for chunks.

    Splits at paragraph > sentence > word boundaries.
    """
    if not text:
        return []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)

        if end < text_len:
            # Try to find a paragraph break
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + target // 2:
                end = para_break + 2
            else:
                # Try sentence break
                search_region = text[start:end]
                sentences = list(SENTENCE_RE.finditer(search_region))
                if sentences and sentences[-1].start() > target // 2:
                    end = start + sentences[-1].end()
                else:
                    # Try word break
                    space = text.rfind(" ", start, end)
                    if space > start + target // 2:
                        end = space + 1

        chunks.append((start, end))

        # Next chunk starts with overlap
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def _detect_language(text: str) -> str:
    """Detect language of a text chunk. Returns 'english' or 'french'."""
    try:
        from langdetect import detect

        lang = detect(text)
        if lang == "fr":
            return "french"
        return "english"
    except Exception:
        return "english"


def _find_page_range(
    char_start: int,
    char_end: int,
    page_offsets: list[tuple[int, int, int]],
) -> tuple[int, int]:
    """Given char range, find page_start and page_end.

    page_offsets: list of (page_num, global_char_start, global_char_end)
    """
    page_start = page_offsets[0][0]
    page_end = page_offsets[-1][0]

    for pnum, pstart, pend in page_offsets:
        if pstart <= char_start < pend:
            page_start = pnum
            break

    for pnum, pstart, pend in page_offsets:
        if pstart < char_end <= pend:
            page_end = pnum
            break

    return page_start, page_end


def run_chunk(version_id: uuid.UUID) -> None:
    """Split extracted text into overlapping chunks."""
    if not mark_stage_running(version_id, JobStage.chunk):
        return

    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        pages = (
            session.execute(
                select(DocumentPage).where(DocumentPage.version_id == version_id).order_by(DocumentPage.page_num)
            )
            .scalars()
            .all()
        )

        if not pages:
            logger.warning("No pages to chunk for version %s", version_id)
            session.close()
            mark_stage_done(version_id, JobStage.chunk)
            return

        # Concatenate all page text with page boundary tracking
        full_text = ""
        page_offsets: list[tuple[int, int, int]] = []  # (page_num, start, end)
        page_ocr_info: list[tuple[int, bool, float | None]] = []  # (page_num, ocr_used, confidence)

        for page in pages:
            start = len(full_text)
            full_text += page.page_text
            end = len(full_text)
            page_offsets.append((page.page_num, start, end))
            page_ocr_info.append((page.page_num, page.ocr_used, page.ocr_confidence))
            full_text += "\n"  # separator between pages

        # Remove trailing newline
        full_text = full_text.rstrip()

        # Delete existing chunks (idempotency)
        existing = session.execute(select(Chunk).where(Chunk.version_id == version_id)).scalars().all()
        for c in existing:
            session.delete(c)
        session.flush()

        # Split into chunks
        settings = get_settings()
        chunk_ranges = _split_text(full_text, target=settings.chunk_target_size, overlap=settings.chunk_overlap)

        for i, (char_start, char_end) in enumerate(chunk_ranges):
            chunk_text = full_text[char_start:char_end]
            if not chunk_text.strip():
                continue

            page_start, page_end = _find_page_range(char_start, char_end, page_offsets)
            language = _detect_language(chunk_text)

            # Determine OCR info for this chunk's page range
            ocr_used = False
            ocr_confidence = None
            confidences = []
            for pnum, ocr, conf in page_ocr_info:
                if page_start <= pnum <= page_end:
                    if ocr:
                        ocr_used = True
                    if conf is not None:
                        confidences.append(conf)
            if confidences:
                ocr_confidence = sum(confidences) / len(confidences)

            chunk = Chunk(
                version_id=version_id,
                doc_id=version.doc_id,
                chunk_num=i,
                page_start=page_start,
                page_end=page_end,
                char_start=char_start,
                char_end=char_end,
                chunk_text=chunk_text,
                language=language,
                ocr_used=ocr_used,
                ocr_confidence=ocr_confidence,
            )
            session.add(chunk)

        session.commit()
        logger.info("Created %d chunks for version %s", len(chunk_ranges), version_id)
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.chunk)
