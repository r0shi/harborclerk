"""Summarize stage — generate adaptive document summary from all chunks."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.summarize import generate_summary
from harbor_clerk.models import Chunk, DocumentVersion
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_summarize(version_id: uuid.UUID) -> None:
    """Generate a summary for the document version from all its chunks."""
    if not mark_stage_running(version_id, JobStage.summarize):
        return

    session = get_sync_session()
    try:
        chunks = (
            session.execute(select(Chunk.chunk_text).where(Chunk.version_id == version_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )
        if chunks:
            try:
                summary, model_used = generate_summary(list(chunks))
            except Exception:
                logger.warning("Summary generation failed for %s", version_id, exc_info=True)
                summary, model_used = None, None

            if summary:
                version = session.execute(
                    select(DocumentVersion).where(DocumentVersion.version_id == version_id)
                ).scalar_one()
                version.summary = summary
                version.summary_model = model_used
                session.commit()
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.summarize)
