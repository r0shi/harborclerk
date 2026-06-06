"""OCR stage — Tesseract on images or scanned PDFs."""

import io as _io
import logging
import os
import uuid
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
from PIL import Image
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.storage import get_storage
from harbor_clerk.worker.ocr_languages import get_ocr_languages_for_doc
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

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
    """Run OCR on pages that need it."""
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

        # Read from storage if available; fall back to source_path for watched folder docs
        if doc.original_object_key:
            storage = get_storage()
            response = storage.get_object(doc.original_bucket, doc.original_object_key)
            data = response.read()
        elif doc.source_path and os.path.exists(doc.source_path):
            data = Path(doc.source_path).read_bytes()
        else:
            raise RuntimeError(f"No source for doc {doc_id}")

        mime = (doc.mime_type or "").lower()

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

            # Race check before writing results
            if not check_pipeline_seq(session, doc_id, worker_seq):
                logger.info("ocr: pipeline_seq bumped during processing for %s, aborting write", doc_id)
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
            session.commit()
            publish_job_event(doc_id, "ocr", "running", progress=1, total=1)

        elif mime == "application/pdf" or (doc.original_object_key or doc.source_path or "").endswith(".pdf"):
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
            for i, img in enumerate(images):
                page_num = i + 1
                # Convert PIL image to bytes for OCR
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()

                text, confidence = _ocr_image_bytes(img_bytes, lang=ocr_lang)
                ocr_results.append((page_num, text, confidence))

            # Race check before writing results
            if not check_pipeline_seq(session, doc_id, worker_seq):
                logger.info("ocr: pipeline_seq bumped during processing for %s, aborting write", doc_id)
                return

            for i, (page_num, text, confidence) in enumerate(ocr_results):
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

                # Merge OCR text with existing text
                if page.page_text and text:
                    page.page_text = page.page_text + "\n\n--- OCR ---\n\n" + text
                elif text:
                    page.page_text = text

                page.ocr_used = True
                page.ocr_confidence = confidence
                total_chars += len(page.page_text)

                job.progress_current = i + 1
                session.commit()
                publish_job_event(doc_id, "ocr", "running", progress=i + 1, total=len(images))

            doc.extracted_chars = total_chars
            doc.ocr_languages_used = ocr_iso
            session.commit()
        else:
            logger.warning("OCR requested for unsupported mime type: %s", mime)

    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.ocr, worker_seq=worker_seq)
