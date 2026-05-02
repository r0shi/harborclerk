"""Embed stage — call embedder service to generate vectors for chunks."""

import logging
import uuid

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

BATCH_SIZE = 64


def run_embed(doc_id: uuid.UUID) -> None:
    """Generate embeddings for all chunks of a doc."""
    if not mark_stage_running(doc_id, JobStage.embed):
        return

    settings = get_settings()
    session = get_sync_session()
    try:
        # Load worker_seq up front so mark_stage_done and per-batch commits can race-check.
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc is None:
            logger.info("embed: doc %s no longer exists, skipping", doc_id)
            return
        worker_seq = doc.pipeline_seq

        # Load chunks missing embeddings
        chunks = (
            session.execute(
                select(Chunk).where(Chunk.doc_id == doc_id, Chunk.embedding.is_(None)).order_by(Chunk.chunk_num)
            )
            .scalars()
            .all()
        )

        if not chunks:
            logger.info("No chunks to embed for doc %s", doc_id)
            session.close()
            mark_stage_done(doc_id, JobStage.embed, worker_seq=worker_seq)
            return

        # Update job progress total
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == JobStage.embed,
            )
        ).scalar_one()
        job.progress_total = len(chunks)
        session.commit()

        # Process in batches. Re-check pipeline_seq before each batch commit so a
        # watcher reprocess that deletes our chunks doesn't make us write
        # embeddings to FK-orphaned rows or burn embedder calls on stale content.
        # The new pipeline run will compute fresh embeddings on its own chunks.
        processed = 0
        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            texts = [c.chunk_text for c in batch]

            resp = httpx.post(
                f"{settings.embedder_url}/embed",
                json={"texts": texts, "task": "passage"},
                timeout=120,
            )
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]

            if not check_pipeline_seq(session, doc_id, worker_seq):
                logger.info("embed: pipeline_seq bumped during processing for %s, aborting", doc_id)
                return

            for chunk, emb in zip(batch, embeddings):
                chunk.embedding = emb

            processed += len(batch)
            job.progress_current = processed
            session.commit()
            publish_job_event(
                doc_id,
                "embed",
                "running",
                progress=processed,
                total=len(chunks),
            )

        logger.info("Embedded %d chunks for doc %s", len(chunks), doc_id)
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.embed, worker_seq=worker_seq)
