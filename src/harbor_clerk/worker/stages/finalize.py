"""Finalize stage — mark document as ready, mark related uploads done."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Chunk, Document, DocumentLink, DocumentPage, Upload
from harbor_clerk.models.enums import JobStage, PipelineStatus
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)


def _resolve_link(target_title: str, candidates_by_name: dict[str, list[uuid.UUID]]) -> uuid.UUID | None:
    """Match a parsed wikilink target against the active corpus.

    ``target_title`` is the parsed name from a ``[[…]]`` capture (already
    lowercased + stripped by ``parse_wikilinks``, but we re-normalize
    defensively).

    ``candidates_by_name`` maps lowercase-stripped names to a list of doc_ids
    that match that name. A name is either a document's ``canonical_filename``
    stem (lowercased) or its ``title`` (lowercased). The caller builds this
    map from the active corpus.

    Returns the unique matching ``doc_id``, or ``None`` if no match or the
    name is ambiguous (two or more docs share it).
    """
    if not target_title:
        return None
    key = target_title.strip().lower()
    if not key:
        return None
    matches = candidates_by_name.get(key, [])
    if len(matches) == 1:
        return matches[0]
    return None


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

        # --- Wikilink resolution ---
        # Build a name→doc_ids map across all active docs (this doc included,
        # because incoming links from this doc to itself are valid). When a
        # doc's stem and title normalize to the same string (e.g. "Note A.md"
        # with title "Note A"), only add the doc_id once for that name —
        # otherwise _resolve_link sees a duplicated list of length >1 and
        # mis-classifies a unique match as ambiguous.
        all_active = session.execute(
            select(Document.doc_id, Document.canonical_filename, Document.title).where(Document.status == "active")
        ).all()
        candidates_by_name: dict[str, list[uuid.UUID]] = {}

        def _add_candidate(name: str, did: uuid.UUID) -> None:
            bucket = candidates_by_name.setdefault(name, [])
            if did not in bucket:
                bucket.append(did)

        for did, fname, title in all_active:
            if fname:
                stem = fname.rsplit(".", 1)[0].strip().lower()
                if stem:
                    _add_candidate(stem, did)
            if title:
                t = title.strip().lower()
                if t:
                    _add_candidate(t, did)

        # Resolve this doc's outgoing unresolved links.
        outgoing = (
            session.execute(
                select(DocumentLink).where(DocumentLink.src_doc_id == doc_id, DocumentLink.resolved.is_(False))
            )
            .scalars()
            .all()
        )
        for link in outgoing:
            resolved_id = _resolve_link(link.target_title, candidates_by_name)
            if resolved_id is not None:
                link.target_doc_id = resolved_id
                link.resolved = True

        # Re-resolve dangling links pointing AT this doc. Compute the names
        # this doc matches; find unresolved links whose target_title matches
        # one of them; resolve if the name is unique (i.e. only this doc has it).
        my_names: set[str] = set()
        if doc.canonical_filename:
            stem = doc.canonical_filename.rsplit(".", 1)[0].strip().lower()
            if stem:
                my_names.add(stem)
        if doc.title:
            t = doc.title.strip().lower()
            if t:
                my_names.add(t)

        # Pre-initialize so the summary log below is safe when my_names is empty.
        dangling: list[DocumentLink] = []
        if my_names:
            dangling = (
                session.execute(
                    select(DocumentLink).where(
                        DocumentLink.resolved.is_(False),
                        DocumentLink.target_title.in_(my_names),
                        DocumentLink.src_doc_id != doc_id,  # outgoing already handled above
                    )
                )
                .scalars()
                .all()
            )
            for link in dangling:
                resolved_id = _resolve_link(link.target_title, candidates_by_name)
                if resolved_id is not None:
                    link.target_doc_id = resolved_id
                    link.resolved = True

        if outgoing or (my_names and dangling):
            logger.info(
                "Resolved %d outgoing + %d dangling wikilinks for doc %s",
                sum(1 for link in outgoing if link.resolved),
                sum(1 for link in (dangling if my_names else []) if link.resolved),
                doc_id,
            )

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
        worker_seq=worker_seq,
        page_count=page_count,
        chunk_count=chunk_count,
    )
