"""OCR stage — Tesseract on images or scanned PDFs."""

import io as _io
import logging
import uuid
from contextlib import ExitStack

import pypdfium2 as pdfium
import pytesseract
from PIL import Image
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.watched_source import read_document_bytes
from harbor_clerk.worker.ocr_languages import get_ocr_languages_for_doc
from harbor_clerk.worker.pipeline import (
    alarm_deferred,
    mark_stage_done,
    mark_stage_running,
    stage_run_is_current,
)

logger = logging.getLogger(__name__)

DPI = 300


def _ocr_image_bytes(image_data: bytes, lang: str) -> tuple[str, float]:
    """OCR a single image with the given Tesseract ``-l`` argument.

    Returns ``(text, average_word_confidence)``. The confidence is the
    mean of non-empty Tesseract word confidences in [0, 100]; 0.0 when
    no words were extracted.
    """
    img = Image.open(_io.BytesIO(image_data))
    # Get detailed data for confidence
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    text = pytesseract.image_to_string(img, lang=lang)

    # Compute average confidence from non-empty words
    confs = [int(c) for c, t in zip(data["conf"], data["text"]) if t.strip() and int(c) >= 0]
    avg_conf = sum(confs) / len(confs) if confs else 0.0

    return text.strip(), avg_conf


OCR_MARKER = "\n\n--- OCR ---\n\n"


def _pdf_to_images(data: bytes) -> list[Image.Image]:
    """Convert PDF pages to PIL Images using pypdfium2."""
    pdf = pdfium.PdfDocument(data)
    images = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=DPI / 72)
            images.append(bitmap.to_pil())
    finally:
        pdf.close()
    return images


def run_ocr(doc_id: uuid.UUID, *, worker_seq: int | None = None) -> None:
    """Run OCR on pages that need it.

    A PDF's page writes are one transaction, committed once: a run stopped part
    way (a cancel, a shutdown deadline, a crash) leaves every page as extract
    wrote it, so a rerun appends its OCR to clean text and never twice. From
    that commit to mark_stage_done the worker's alarm is held off, so a deadline
    cannot land between them and rerun OCR over pages already written. A single
    image replaces its page's text, so a rerun is harmless and needs no hold.
    """
    with ExitStack() as hold_alarm:
        _run_ocr(doc_id, worker_seq, hold_alarm)


def _run_ocr(doc_id: uuid.UUID, worker_seq: int | None, hold_alarm: ExitStack) -> None:
    if not mark_stage_running(doc_id, JobStage.ocr, worker_seq=worker_seq):
        return

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        if worker_seq is None:
            worker_seq = doc.pipeline_seq

        # If OCR not needed, just mark done
        if not doc.needs_ocr:
            session.close()
            mark_stage_done(doc_id, JobStage.ocr, worker_seq=worker_seq)
            return

        # Read as the extract stage does (watched_source.read_document_bytes).
        data, in_place = read_document_bytes(session, doc)

        # The type of what was read: for a file read in place, the extract stage has
        # already set doc.mime_type from the file (and its magic bytes), and the name
        # is the file's, never a stored original's that may describe an older upload.
        mime = (doc.mime_type or "").lower()
        name = (in_place or doc.original_object_key or doc.source_path or "").lower()

        # Resolve which Tesseract languages to use for this doc.
        # Honors the operator's enabled_languages preference, filtered to
        # those with installed packs. English is always included as the
        # safe fallback. See harbor_clerk/worker/ocr_languages.py for
        # the resolution rules.
        ocr_iso, ocr_lang = get_ocr_languages_for_doc(session)
        logger.info("OCR for doc %s: using languages %s (-l %s)", doc_id, ocr_iso, ocr_lang)

        # Update job progress total
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == JobStage.ocr,
                IngestionJob.pipeline_seq == worker_seq,
            )
        ).scalar_one()

        if mime in ("image/jpeg", "image/png", "image/tiff"):
            # Single image OCR
            job.progress_total = 1
            session.commit()

            text, confidence = _ocr_image_bytes(data, lang=ocr_lang)

            # Race check before writing results: the seq, a cancel, or a newer claim.
            if not stage_run_is_current(session, doc_id, JobStage.ocr, worker_seq):
                logger.info("ocr: run for %s cancelled, re-enqueued or superseded, aborting write", doc_id)
                session.close()
                mark_stage_done(doc_id, JobStage.ocr, worker_seq=worker_seq)  # declines likewise
                return

            page = session.execute(
                select(DocumentPage).where(
                    DocumentPage.doc_id == doc_id,
                    DocumentPage.page_num == 1,
                )
            ).scalar_one()
            page.page_text = text
            page.ocr_used = True
            page.ocr_confidence = confidence
            doc.extracted_chars = len(text)
            doc.ocr_languages_used = ocr_iso

            job.progress_current = 1
            # No alarm hold here, unlike the PDF path: this write replaces the
            # page text whole, so a rerun after the commit writes the same page.
            session.commit()
            publish_job_event(doc_id, "ocr", "running", progress=1, total=1)

        elif mime == "application/pdf" or name.endswith(".pdf"):
            # PDF → page images → OCR (using pypdfium2)
            images = _pdf_to_images(data)
            job.progress_total = len(images)
            session.commit()

            pages = (
                session.execute(
                    select(DocumentPage).where(DocumentPage.doc_id == doc_id).order_by(DocumentPage.page_num)
                )
                .scalars()
                .all()
            )

            total_chars = 0
            ocr_results = []
            stopped = False
            for i, img in enumerate(images):
                # A page takes seconds and a scan can run to the 7200 s stage
                # timeout: ask between pages whether the run is still wanted, so
                # a cancel or queue clear stops it here (#620's sibling).
                if not stage_run_is_current(session, doc_id, JobStage.ocr, worker_seq):
                    stopped = True
                    break
                page_num = i + 1
                # Convert PIL image to bytes for OCR
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()

                text, confidence = _ocr_image_bytes(img_bytes, lang=ocr_lang)
                ocr_results.append((page_num, text, confidence))

            # Race check before writing results
            if stopped:
                logger.info(
                    "ocr: run for %s cancelled or superseded after %d of %d pages; not writing",
                    doc_id,
                    len(ocr_results),
                    len(images),
                )
                ocr_results = []
            elif not stage_run_is_current(session, doc_id, JobStage.ocr, worker_seq):
                logger.info("ocr: run for %s cancelled, re-enqueued or superseded, aborting write", doc_id)
                session.close()
                mark_stage_done(doc_id, JobStage.ocr, worker_seq=worker_seq)  # declines likewise
                return

            # Every page in one transaction, committed once below: no page is
            # written unless all of them are.
            for page_num, text, confidence in ocr_results:
                # Find or create page
                page = None
                for p in pages:
                    if p.page_num == page_num:
                        page = p
                        break
                if page is None:
                    page = DocumentPage(
                        doc_id=doc_id,
                        page_num=page_num,
                        page_text="",
                    )
                    session.add(page)
                    session.flush()

                # Merge OCR text with existing text (nothing to add when OCR found none)
                if page.page_text and text:
                    page.page_text = page.page_text + OCR_MARKER + text
                elif text:
                    page.page_text = text

                page.ocr_used = True
                page.ocr_confidence = confidence
                total_chars += len(page.page_text)

            if not stopped:
                job.progress_current = len(ocr_results)
                doc.extracted_chars = total_chars
                doc.ocr_languages_used = ocr_iso
                hold_alarm.enter_context(alarm_deferred())
                session.commit()
                publish_job_event(doc_id, "ocr", "running", progress=len(ocr_results), total=len(images))
        else:
            logger.warning("OCR requested for unsupported mime type: %s", mime)

    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.ocr, worker_seq=worker_seq)
