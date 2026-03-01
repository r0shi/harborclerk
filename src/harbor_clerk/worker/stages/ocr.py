"""OCR stage — Tesseract on images or scanned PDFs."""

import io as _io
import logging
import uuid

import pypdfium2 as pdfium
import pytesseract
from PIL import Image
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import DocumentPage, DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.storage import get_storage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

TESSERACT_LANG = "eng+fra"
DPI = 300


def _ocr_image_bytes(image_data: bytes) -> tuple[str, float]:
    """OCR a single image, return (text, confidence)."""
    img = Image.open(_io.BytesIO(image_data))
    # Get detailed data for confidence
    data = pytesseract.image_to_data(img, lang=TESSERACT_LANG, output_type=pytesseract.Output.DICT)
    text = pytesseract.image_to_string(img, lang=TESSERACT_LANG)

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


def run_ocr(version_id: uuid.UUID) -> None:
    """Run OCR on pages that need it."""
    if not mark_stage_running(version_id, JobStage.ocr):
        return

    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        # If OCR not needed, just mark done
        if not version.needs_ocr:
            session.close()
            mark_stage_done(version_id, JobStage.ocr)
            return

        # Download file
        storage = get_storage()
        response = storage.get_object(version.original_bucket, version.original_object_key)
        data = response.read()

        mime = (version.mime_type or "").lower()

        # Update job progress total
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == JobStage.ocr,
            )
        ).scalar_one()

        if mime in ("image/jpeg", "image/png", "image/tiff"):
            # Single image OCR
            job.progress_total = 1
            session.commit()

            text, confidence = _ocr_image_bytes(data)

            page = session.execute(
                select(DocumentPage).where(
                    DocumentPage.version_id == version_id,
                    DocumentPage.page_num == 1,
                )
            ).scalar_one()
            page.page_text = text
            page.ocr_used = True
            page.ocr_confidence = confidence
            version.extracted_chars = len(text)

            job.progress_current = 1
            session.commit()
            publish_job_event(version_id, "ocr", "running", progress=1, total=1)

        elif mime == "application/pdf" or version.original_object_key.endswith(".pdf"):
            # PDF → page images → OCR (using pypdfium2)
            images = _pdf_to_images(data)
            job.progress_total = len(images)
            session.commit()

            pages = (
                session.execute(
                    select(DocumentPage).where(DocumentPage.version_id == version_id).order_by(DocumentPage.page_num)
                )
                .scalars()
                .all()
            )

            total_chars = 0
            for i, img in enumerate(images):
                page_num = i + 1
                # Convert PIL image to bytes for OCR
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()

                text, confidence = _ocr_image_bytes(img_bytes)

                # Find or create page
                page = None
                for p in pages:
                    if p.page_num == page_num:
                        page = p
                        break
                if page is None:
                    page = DocumentPage(
                        version_id=version_id,
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
                publish_job_event(version_id, "ocr", "running", progress=i + 1, total=len(images))

            version.extracted_chars = total_chars
            session.commit()
        else:
            logger.warning("OCR requested for unsupported mime type: %s", mime)

    finally:
        session.close()

    mark_stage_done(version_id, JobStage.ocr)
