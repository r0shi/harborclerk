"""Embed stage — call embedder service to generate vectors for chunks."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.embedder_client import embed_texts
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running, stage_run_is_current

logger = logging.getLogger(__name__)

BATCH_SIZE = 64


def run_embed(doc_id: uuid.UUID, *, worker_seq: int | None = None) -> None:
    """Generate embeddings for all chunks of a doc."""
    if not mark_stage_running(doc_id, JobStage.embed, worker_seq=worker_seq):
        return

    session = get_sync_session()
    try:
        # Load worker_seq up front so mark_stage_done and per-batch commits can race-check.
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc is None:
            logger.info("embed: doc %s no longer exists, skipping", doc_id)
            return
        if worker_seq is None:
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
                IngestionJob.pipeline_seq == worker_seq,
            )
        ).scalar_one()
        job.progress_total = len(chunks)
        session.commit()

        # Process in batches, asking before each embedder call and again before
        # each write whether this run is still wanted (stage_run_is_current):
        # - a watcher reprocess bumps pipeline_seq and deletes our chunks, so
        #   writing would hit FK-orphaned rows and calling would burn the
        #   embedder on stale content; the new run embeds its own chunks;
        # - a cancel or queue clear sets the job row to error, and a 57k-chunk
        #   document once kept the embedder at 20-30 GB for 25 minutes after
        #   its cancel because only the seq was checked (#620).
        # Either way the loop stops within one batch and falls through to
        # mark_stage_done, which declines both (seq mismatch, errored row).
        processed = 0
        stopped = False
        for batch_start in range(0, len(chunks), BATCH_SIZE):
            if not stage_run_is_current(session, doc_id, JobStage.embed, worker_seq):
                stopped = True
                break

            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            texts = [c.chunk_text for c in batch]

            embeddings = embed_texts(texts, task="passage", timeout=120)

            if not stage_run_is_current(session, doc_id, JobStage.embed, worker_seq):
                stopped = True
                break

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

        if stopped:
            logger.info(
                "embed: run for %s cancelled or superseded; stopped after %d of %d chunks",
                doc_id,
                processed,
                len(chunks),
            )
        else:
            logger.info("Embedded %d chunks for doc %s", len(chunks), doc_id)
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.embed, worker_seq=worker_seq)
