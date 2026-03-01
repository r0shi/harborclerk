"""Finalize stage — mark version as ready, update document."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentPage, DocumentVersion, Upload
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_finalize(version_id: uuid.UUID) -> None:
    """Complete ingestion: set version ready, update document.latest_version_id, mark upload done."""
    if not mark_stage_running(version_id, JobStage.finalize):
        return

    doc_id: uuid.UUID | None = None
    page_count = 0
    chunk_count = 0

    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        # Update document.latest_version_id
        doc = session.execute(select(Document).where(Document.doc_id == version.doc_id)).scalar_one()
        doc.latest_version_id = version_id
        doc.updated_at = datetime.now(UTC)

        # Mark related uploads as done
        uploads = session.execute(select(Upload).where(Upload.version_id == version_id)).scalars().all()
        for u in uploads:
            if u.status == "processing":
                u.status = "done"

        session.commit()

        page_count = session.execute(
            select(func.count()).select_from(DocumentPage).where(DocumentPage.version_id == version_id)
        ).scalar_one()
        chunk_count = session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.version_id == version_id)
        ).scalar_one()
        doc_id = doc.doc_id

        logger.info("Finalized version %s for document %s", version_id, doc_id)
    finally:
        session.close()

    mark_stage_done(
        version_id,
        JobStage.finalize,
        doc_id=doc_id,
        page_count=page_count,
        chunk_count=chunk_count,
    )
