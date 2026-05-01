"""Finalize stage — mark document as ready, mark related uploads done."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, Upload
from harbor_clerk.models.enums import JobStage, PipelineStatus
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_finalize(doc_id: uuid.UUID) -> None:
    """Complete ingestion: set doc ready, mark upload done."""
    if not mark_stage_running(doc_id, JobStage.finalize):
        return

    page_count = 0
    chunk_count = 0

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        worker_seq = doc.pipeline_seq

        page_count = session.execute(
            select(func.count()).select_from(DocumentPage).where(DocumentPage.doc_id == doc_id)
        ).scalar_one()
        chunk_count = session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
        ).scalar_one()

        # Race check before writing results
        if not check_pipeline_seq(session, doc_id, worker_seq):
            logger.info("finalize: pipeline_seq bumped during processing for %s, aborting write", doc_id)
            return

        doc.pipeline_status = PipelineStatus.ready
        doc.updated_at = datetime.now(UTC)

        # Mark related uploads as done
        uploads = session.execute(select(Upload).where(Upload.doc_id == doc_id)).scalars().all()
        for u in uploads:
            if u.status == "processing":
                u.status = "done"

        session.commit()
        logger.info("Finalized doc %s (%d pages, %d chunks)", doc_id, page_count, chunk_count)
    finally:
        session.close()

    mark_stage_done(
        doc_id,
        JobStage.finalize,
        doc_id=doc_id,
        page_count=page_count,
        chunk_count=chunk_count,
    )
