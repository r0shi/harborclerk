"""Entities stage — extract named entities from chunks using spaCy NER."""

import logging
import uuid

from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, DocumentVersion
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage
from harbor_clerk.worker.ner import extract_entities_batch
from harbor_clerk.worker.pipeline import mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def run_entities(version_id: uuid.UUID) -> None:
    """Extract named entities from all chunks for this version."""
    if not mark_stage_running(version_id, JobStage.entities):
        return

    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        chunks = (
            session.execute(select(Chunk).where(Chunk.version_id == version_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )

        if not chunks:
            logger.warning("No chunks to extract entities from for version %s", version_id)
            session.close()
            mark_stage_done(version_id, JobStage.entities, entity_count=0)
            return

        # Delete existing entities for idempotency
        existing = session.execute(select(Entity).where(Entity.version_id == version_id)).scalars().all()
        for e in existing:
            session.delete(e)
        session.flush()

        # Batch NER
        batch_input = [(c.chunk_text, c.language or "english") for c in chunks]
        batch_results = extract_entities_batch(batch_input)

        entity_count = 0
        for chunk, ents in zip(chunks, batch_results):
            for ent in ents:
                session.add(
                    Entity(
                        version_id=version_id,
                        chunk_id=chunk.chunk_id,
                        doc_id=version.doc_id,
                        entity_text=ent.text,
                        entity_type=ent.type,
                        start_char=ent.start_char,
                        end_char=ent.end_char,
                    )
                )
                entity_count += 1

        session.commit()
        logger.info(
            "Extracted %d entities from %d chunks for version %s",
            entity_count,
            len(chunks),
            version_id,
        )
    finally:
        session.close()

    mark_stage_done(version_id, JobStage.entities, entity_count=entity_count)
