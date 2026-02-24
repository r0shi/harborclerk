"""Embed stage — call embedder service to generate vectors for chunks."""

import logging
import uuid

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Chunk, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

BATCH_SIZE = 64


def run_embed(version_id: uuid.UUID) -> None:
    """Generate embeddings for all chunks of a version."""
    if not mark_stage_running(version_id, JobStage.embed):
        return

    settings = get_settings()
    session = get_sync_session()
    try:
        # Load chunks missing embeddings
        chunks = session.execute(
            select(Chunk)
            .where(Chunk.version_id == version_id, Chunk.embedding.is_(None))
            .order_by(Chunk.chunk_num)
        ).scalars().all()

        if not chunks:
            logger.info("No chunks to embed for version %s", version_id)
            session.close()
            mark_stage_done(version_id, JobStage.embed)
            return

        # Update job progress total
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == JobStage.embed,
            )
        ).scalar_one()
        job.progress_total = len(chunks)
        session.commit()

        # Process in batches
        processed = 0
        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            texts = [c.chunk_text for c in batch]

            resp = httpx.post(
                f"{settings.embedder_url}/embed",
                json={"texts": texts},
                timeout=120,
            )
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]

            for chunk, emb in zip(batch, embeddings):
                chunk.embedding = emb

            processed += len(batch)
            job.progress_current = processed
            session.commit()
            publish_job_event(
                version_id, "embed", "running",
                progress=processed, total=len(chunks),
            )

        logger.info("Embedded %d chunks for version %s", len(chunks), version_id)
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.embed)
