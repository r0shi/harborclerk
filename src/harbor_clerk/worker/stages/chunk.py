"""Chunk stage — split page text into overlapping chunks."""

import logging
import re
import uuid

from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentHeading, DocumentPage
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

# Sentence boundary pattern
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _find_code_fence_ranges(text: str) -> list[tuple[int, int]]:
    """Return char ranges covering each fenced code block in ``text``.

    Each range is ``(start, end)`` where ``start`` is the char offset of the
    opening fence line's first char and ``end`` is the char offset just past
    the closing fence line (i.e., the start of the next line, or ``len(text)``
    if the closing fence is unterminated or at end-of-text).

    Recognizes both backtick (``` ``` ```) and tilde (``~~~``) fences.
    """
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_start_char = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if not in_fence and is_fence:
            in_fence = True
            fence_start_char = line_starts[i]
        elif in_fence and is_fence:
            in_fence = False
            ranges.append((fence_start_char, line_starts[i + 1]))

    # Unterminated fence at EOF — treat as extending through the rest of the text.
    if in_fence:
        ranges.append((fence_start_char, len(text)))

    return ranges


def _find_heading_positions_in_text(text: str, headings: list[dict]) -> list[int]:
    """Locate each heading's ``title`` at a line start in ``text``.

    Returns a list of char offsets (one per heading whose title is found),
    in the order the headings were given. A low-water mark advances after
    each match so repeated titles match in document order.

    Headings with empty titles or titles that don't appear at a line start
    in ``text`` are silently skipped.
    """
    if not text or not headings:
        return []

    positions: list[int] = []
    low_water = 0
    for h in headings:
        title = h.get("title") or ""
        if not title:
            continue
        # Find the title at a line start (preceded by '\n' or at text start).
        search_from = low_water
        while search_from < len(text):
            idx = text.find(title, search_from)
            if idx < 0:
                break
            if idx == 0 or text[idx - 1] == "\n":
                positions.append(idx)
                low_water = idx + len(title)
                break
            search_from = idx + 1

    return positions


def _split_text(
    text: str,
    target: int = 1000,
    overlap: int = 150,
    heading_positions: list[int] | None = None,
    code_fence_ranges: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Return list of ``(char_start, char_end)`` for chunks.

    Break-preference order (highest to lowest priority):
      1. A heading position in ``heading_positions`` that falls in the upper
         half of the target window (``start + target // 2 < pos <= start + target``).
      2. A paragraph break (``\\n\\n``) in the upper half of the window.
      3. A sentence boundary in the upper half of the window.
      4. A word boundary in the upper half of the window.
      5. Hard cut at ``start + target``.

    Code-fence protection: if the chosen end falls strictly inside any range
    in ``code_fence_ranges``, the end is pushed to the range's far edge — the
    chunk is extended to cover the entire fence. (Headings do not appear inside
    fences in practice, but the protection runs after heading selection too.)
    """
    if not text:
        return []
    heading_positions = heading_positions or []
    code_fence_ranges = code_fence_ranges or []

    chunks: list[tuple[int, int]] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)

        if end < text_len:
            # Preference 1: a heading boundary in the upper half of the window.
            heading_break: int | None = None
            half = start + target // 2
            for hpos in heading_positions:
                if half < hpos <= end:
                    heading_break = hpos  # rightmost match wins (closest to end)

            if heading_break is not None:
                end = heading_break
            else:
                # Existing paragraph > sentence > word fallback (unchanged).
                para_break = text.rfind("\n\n", start, end)
                if para_break > start + target // 2:
                    end = para_break + 2
                else:
                    search_region = text[start:end]
                    sentences = list(SENTENCE_RE.finditer(search_region))
                    if sentences and sentences[-1].start() > target // 2:
                        end = start + sentences[-1].end()
                    else:
                        space = text.rfind(" ", start, end)
                        if space > start + target // 2:
                            end = space + 1

            # Code-fence protection: never end inside a fence. The loop runs to
            # completion (no break) so that pushing `end` past one fence can land
            # it inside an adjacent fence — a later iteration catches that too.
            # Requires `code_fence_ranges` to be sorted by start offset, which
            # `_find_code_fence_ranges` always ensures (it iterates lines in
            # document order). For reversed/unsorted input, a single forward pass
            # is not sufficient — callers must pre-sort.
            for fstart, fend in code_fence_ranges:
                if fstart < end < fend:
                    # Deliberate oversized-chunk: extending past the fence is
                    # acceptable; splitting a code block is worse than a chunk
                    # that exceeds the target by up to one fence's length.
                    end = fend

        chunks.append((start, end))

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


def run_chunk(doc_id: uuid.UUID, *, worker_seq: int | None = None) -> None:
    """Split extracted text into overlapping chunks."""
    if not mark_stage_running(doc_id, JobStage.chunk, worker_seq=worker_seq):
        return

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        if worker_seq is None:
            worker_seq = doc.pipeline_seq

        pages = (
            session.execute(select(DocumentPage).where(DocumentPage.doc_id == doc_id).order_by(DocumentPage.page_num))
            .scalars()
            .all()
        )

        if not pages:
            logger.warning("No pages to chunk for doc %s", doc_id)
            session.close()
            mark_stage_done(doc_id, JobStage.chunk, worker_seq=worker_seq)
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

        # Look up headings for break-preference. Ordered by position so the
        # search in `_find_heading_positions_in_text` advances monotonically.
        heading_rows = (
            session.execute(
                select(DocumentHeading).where(DocumentHeading.doc_id == doc_id).order_by(DocumentHeading.position)
            )
            .scalars()
            .all()
        )
        heading_dicts = [{"title": h.title} for h in heading_rows]
        heading_positions = _find_heading_positions_in_text(full_text, heading_dicts)
        code_fence_ranges = _find_code_fence_ranges(full_text)

        # Split into chunks (compute before race check)
        settings = get_settings()
        chunk_ranges = _split_text(
            full_text,
            target=settings.chunk_target_size,
            overlap=settings.chunk_overlap,
            heading_positions=heading_positions,
            code_fence_ranges=code_fence_ranges,
        )

        # Race check before writing results
        if not check_pipeline_seq(session, doc_id, worker_seq):
            logger.info("chunk: pipeline_seq bumped during processing for %s, aborting write", doc_id)
            return

        # Delete existing chunks (idempotency)
        existing = session.execute(select(Chunk).where(Chunk.doc_id == doc_id)).scalars().all()
        for c in existing:
            session.delete(c)
        session.flush()

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
                doc_id=doc_id,
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
        logger.info("Created %d chunks for doc %s", len(chunk_ranges), doc_id)
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.chunk, worker_seq=worker_seq)
