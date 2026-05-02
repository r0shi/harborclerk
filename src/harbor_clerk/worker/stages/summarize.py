"""Summarize stage — generate adaptive document summary from all chunks."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.config import refresh_llm_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.summarize import classify_doc_type, generate_summary
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_summarize(doc_id: uuid.UUID) -> None:
    """Generate a summary for the document from all its chunks."""
    if not mark_stage_running(doc_id, JobStage.summarize):
        return

    # Re-read LLM model from config.json in case user changed it via API
    refresh_llm_settings()

    session = get_sync_session()
    try:
        chunks = (
            session.execute(select(Chunk.chunk_text).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )

        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        worker_seq = doc.pipeline_seq

        if chunks:
            # Generate summary (compute before race check)
            try:
                summary, model_used = generate_summary(list(chunks))
            except Exception:
                logger.warning("Summary generation failed for %s", doc_id, exc_info=True)
                summary, model_used = None, None

            # Classify document type (compute before race check)
            doc_type = None
            try:
                doc_type = classify_doc_type(list(chunks), mime_type=doc.mime_type or "")
            except Exception:
                logger.warning("Doc type classification failed for %s", doc_id, exc_info=True)

            # Race check before writing results
            if not check_pipeline_seq(session, doc_id, worker_seq):
                logger.info("summarize: pipeline_seq bumped during processing for %s, aborting write", doc_id)
                return

            # Require visible content — not just truthiness — so a degraded
            # LLM that returns whitespace or zero-width characters can't
            # persist a "blank summary attributed to the current model".
            # generate_summary should already filter these via
            # _has_visible_content, but a check here is a cheap belt-and-
            # suspenders against any future regression.
            if summary and summary.strip():
                doc.summary = summary
                doc.summary_model = model_used

            if doc_type is not None:
                doc.doc_type = doc_type

            session.commit()
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.summarize, worker_seq=worker_seq)
