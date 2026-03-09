"""Summarize stage — generate adaptive document summary from all chunks."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.config import refresh_llm_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.summarize import classify_doc_type, generate_summary
from harbor_clerk.models import Chunk, DocumentVersion
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_summarize(version_id: uuid.UUID) -> None:
    """Generate a summary for the document version from all its chunks."""
    if not mark_stage_running(version_id, JobStage.summarize):
        return

    # Re-read LLM model from config.json in case user changed it via API
    refresh_llm_settings()

    session = get_sync_session()
    try:
        chunks = (
            session.execute(select(Chunk.chunk_text).where(Chunk.version_id == version_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )

        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        if chunks:
            # Generate summary
            try:
                summary, model_used = generate_summary(list(chunks))
            except Exception:
                logger.warning("Summary generation failed for %s", version_id, exc_info=True)
                summary, model_used = None, None

            if summary:
                version.summary = summary
                version.summary_model = model_used

            # Classify document type
            try:
                doc_type = classify_doc_type(list(chunks), mime_type=version.mime_type or "")
                version.doc_type = doc_type
            except Exception:
                logger.warning("Doc type classification failed for %s", version_id, exc_info=True)

            session.commit()
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.summarize)
