"""Entities stage — extract named entities from chunks using spaCy NER."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.ner import extract_entities_batch
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_entities(doc_id: uuid.UUID, *, worker_seq: int | None = None) -> None:
    """Extract named entities from all chunks for this doc."""
    if not mark_stage_running(doc_id, JobStage.entities, worker_seq=worker_seq):
        return

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        if worker_seq is None:
            worker_seq = doc.pipeline_seq

        chunks = session.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_num)).scalars().all()

        if not chunks:
            logger.warning("No chunks to extract entities from for doc %s", doc_id)
            session.close()
            mark_stage_done(doc_id, JobStage.entities, worker_seq=worker_seq, entity_count=0)
            return

        # Batch NER (compute before race check)
        batch_input = [(c.chunk_text, c.language or "english") for c in chunks]
        batch_results = extract_entities_batch(batch_input)

        # Race check before writing results
        if not check_pipeline_seq(session, doc_id, worker_seq):
            logger.info("entities: pipeline_seq bumped during processing for %s, aborting write", doc_id)
            return

        # Delete existing entities for idempotency
        existing = session.execute(select(Entity).where(Entity.doc_id == doc_id)).scalars().all()
        for e in existing:
            session.delete(e)
        session.flush()

        entity_count = 0
        for chunk, ents in zip(chunks, batch_results):
            for ent in ents:
                session.add(
                    Entity(
                        chunk_id=chunk.chunk_id,
                        doc_id=doc_id,
                        entity_text=ent.text,
                        entity_type=ent.type,
                        start_char=ent.start_char,
                        end_char=ent.end_char,
                    )
                )
                entity_count += 1

        session.commit()
        logger.info(
            "Extracted %d entities from %d chunks for doc %s",
            entity_count,
            len(chunks),
            doc_id,
        )
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.entities, worker_seq=worker_seq, entity_count=entity_count)
