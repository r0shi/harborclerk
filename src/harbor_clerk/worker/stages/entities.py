"""Entities stage — extract named entities from chunks using spaCy NER."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.ner import extract_entities_batch
from harbor_clerk.worker.pipeline import (
    mark_stage_done,
    mark_stage_running,
    stage_run_is_current,
)

logger = logging.getLogger(__name__)

# Chunks per NER call. spaCy's nlp.pipe batches internally; this only sets how
# often the stage asks whether it was cancelled. 256 chunks of ~1,000
# characters is seconds of NER, against the stage's 900 s timeout.
NER_BATCH_SIZE = 256


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

        # NER in batches (computed before any write), asking between batches
        # whether the run is still wanted: a cancel or queue clear sets the job
        # row to error without bumping pipeline_seq, and one call over every
        # chunk of a large document ran to the 900 s stage timeout after it
        # (#620's sibling). A stopped or superseded run writes nothing and falls
        # through to mark_stage_done, which declines it (every exit path).
        batch_results: list = []
        stopped = False
        for start in range(0, len(chunks), NER_BATCH_SIZE):
            if not stage_run_is_current(session, doc_id, JobStage.entities, worker_seq):
                stopped = True
                break
            batch = chunks[start : start + NER_BATCH_SIZE]
            batch_results.extend(extract_entities_batch([(c.chunk_text, c.language or "english") for c in batch]))

        # Race check before writing results: the seq, a cancel, or a newer claim.
        if stopped or not stage_run_is_current(session, doc_id, JobStage.entities, worker_seq):
            logger.info("entities: run for %s cancelled or superseded; not writing", doc_id)
            session.close()
            mark_stage_done(doc_id, JobStage.entities, worker_seq=worker_seq, entity_count=0)
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
