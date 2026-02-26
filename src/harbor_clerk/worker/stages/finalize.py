"""Finalize stage — mark version as ready, update document."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.summarize import generate_summary
from harbor_clerk.models import Chunk, Document, DocumentVersion, Upload
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_finalize(version_id: uuid.UUID) -> None:
    """Complete ingestion: set version ready, update document.latest_version_id, mark upload done."""
    if not mark_stage_running(version_id, JobStage.finalize):
        return

    session = get_sync_session()
    try:
        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one()

        # Update document.latest_version_id
        doc = session.execute(
            select(Document).where(Document.doc_id == version.doc_id)
        ).scalar_one()
        doc.latest_version_id = version_id
        doc.updated_at = datetime.now(timezone.utc)

        # Mark related uploads as done
        uploads = (
            session.execute(select(Upload).where(Upload.version_id == version_id))
            .scalars()
            .all()
        )
        for u in uploads:
            if u.status == "processing":
                u.status = "done"

        # Generate document summary from first chunks
        try:
            chunks = (
                session.execute(
                    select(Chunk.chunk_text)
                    .where(Chunk.version_id == version_id)
                    .order_by(Chunk.chunk_num)
                    .limit(5)
                )
                .scalars()
                .all()
            )
            if chunks:
                combined = "\n\n".join(chunks)
                version.summary = generate_summary(combined)
        except Exception:
            logger.warning(
                "Summary generation failed for %s", version_id, exc_info=True
            )

        session.commit()
        logger.info("Finalized version %s for document %s", version_id, version.doc_id)
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.finalize)
