"""Pipeline orchestrator — enqueue stages, advance pipeline, handle failures."""

import logging
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus

logger = logging.getLogger(__name__)

# Cache NER availability at module level (won't change during worker lifetime)
_ner_available: bool | None = None


def _is_ner_available() -> bool:
    global _ner_available
    if _ner_available is None:
        from harbor_clerk.worker.ner import is_ner_available

        _ner_available = is_ner_available()
    return _ner_available


def _doc_filename(doc: Document) -> str:
    """Return the filename portion of doc.source_path or original_object_key, or empty string."""
    if doc.original_object_key:
        return os.path.basename(doc.original_object_key)
    if doc.source_path:
        return os.path.basename(doc.source_path)
    return ""


def check_pipeline_seq(session, doc_id: uuid.UUID, expected_seq: int) -> bool:
    """Return True if the doc's pipeline_seq still matches.

    Workers call this at result-write time to detect a content-change race.
    If the seq has been bumped (e.g., a watcher modify event arrived during
    in-flight extract), the worker should abort writing its results — the
    new ingestion is already queued and will run.

    Usage pattern in stage modules:

        worker_seq = session.query(Document.pipeline_seq).filter_by(doc_id=doc_id).scalar()
        # ... do the work ...
        if not check_pipeline_seq(session, doc_id, worker_seq):
            logger.info("stage X: pipeline_seq bumped during processing for %s, aborting write", doc_id)
            return  # or rollback / raise as appropriate
        # ... write results ...
    """
    current = session.execute(select(Document.pipeline_seq).where(Document.doc_id == doc_id)).scalar()
    return current == expected_seq


# stage → (queue_name, timeout_seconds, pipeline_status_while_running)
STAGE_CONFIG: dict[JobStage, tuple[str, int, PipelineStatus]] = {
    JobStage.extract: ("io", 600, PipelineStatus.extracting),
    JobStage.ocr: ("cpu", 7200, PipelineStatus.ocr_running),
    JobStage.chunk: ("io", 1200, PipelineStatus.chunking),
    JobStage.entities: ("io", 900, PipelineStatus.extracting_entities),
    JobStage.embed: ("cpu", 1800, PipelineStatus.embedding),
    JobStage.summarize: ("llm", 900, PipelineStatus.summarizing),
    JobStage.finalize: ("io", 600, PipelineStatus.finalizing),
}

# Ordered pipeline stages (documentation; actual execution uses fan-out after chunk)
STAGE_ORDER = [
    JobStage.extract,
    JobStage.ocr,
    JobStage.chunk,
    JobStage.entities,
    JobStage.embed,
    JobStage.summarize,
    JobStage.finalize,
]

# Sequential prefix: these stages run one after another
_SEQUENTIAL_STAGES = [JobStage.extract, JobStage.ocr, JobStage.chunk]

# Parallel stages: fan out after chunk, fan in before finalize
_PARALLEL_STAGES = frozenset({JobStage.entities, JobStage.embed, JobStage.summarize})

# Status after stage completes
STAGE_DONE_STATUS: dict[JobStage, PipelineStatus] = {
    JobStage.extract: PipelineStatus.extracted,
    JobStage.ocr: PipelineStatus.ocr_done,
    JobStage.chunk: PipelineStatus.chunked,
    JobStage.entities: PipelineStatus.entities_done,
    JobStage.embed: PipelineStatus.embedded,
    JobStage.summarize: PipelineStatus.summarized,
    JobStage.finalize: PipelineStatus.ready,
}


def enqueue_stage(doc_id: uuid.UUID, stage: JobStage, *, priority: int = 0) -> None:
    """Upsert IngestionJob row as queued and notify workers."""
    queue_name, timeout, running_status = STAGE_CONFIG[stage]

    session = get_sync_session()
    try:
        # Upsert ingestion job
        existing = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status in (JobStatus.queued, JobStatus.running):
                # Already enqueued or running (e.g. concurrent advance_pipeline calls) — skip
                session.close()
                return
            existing.status = JobStatus.queued
            existing.error = None
            existing.progress_current = 0
            existing.progress_total = 0
            existing.started_at = None
            existing.finished_at = None
            existing.priority = priority
        else:
            job = IngestionJob(
                doc_id=doc_id,
                stage=stage,
                status=JobStatus.queued,
                priority=priority,
            )
            session.add(job)

        # Update doc pipeline_status
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        doc.pipeline_status = running_status
        doc.error = None

        session.commit()

        filename = _doc_filename(doc)

        # Notify workers on per-queue channel for instant wakeup
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": f"job_enqueued_{queue_name}", "payload": str(doc_id)},
        )
        session.commit()
    finally:
        session.close()

    publish_job_event(doc_id, stage.value, "queued", filename=filename)
    logger.info("Enqueued %s for doc %s on queue %s", stage.value, doc_id, queue_name)


def reset_jobs(doc_id: uuid.UUID) -> None:
    """Delete all ingestion jobs for a doc so the pipeline starts fresh."""
    session = get_sync_session()
    try:
        jobs = session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id)).scalars().all()
        for j in jobs:
            session.delete(j)
        session.commit()
        logger.info("Reset %d jobs for doc %s", len(jobs), doc_id)
    finally:
        session.close()


def _mark_skipped(
    session,
    doc_id: uuid.UUID,
    stage: JobStage,
    pipeline_status: PipelineStatus,
    filename: str,
    *,
    reason: str | None = None,
    priority: int = 0,
) -> None:
    """Mark a stage as skipped (done without running) and publish the event."""
    now = datetime.now(UTC)
    metrics: dict = {"skipped": True}
    if reason:
        metrics["reason"] = reason

    existing = session.execute(
        select(IngestionJob).where(
            IngestionJob.doc_id == doc_id,
            IngestionJob.stage == stage,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            IngestionJob(
                doc_id=doc_id,
                stage=stage,
                status=JobStatus.done,
                started_at=now,
                finished_at=now,
                metrics=metrics,
                priority=priority,
            )
        )
    else:
        existing.status = JobStatus.done
        existing.finished_at = now
        existing.metrics = metrics
        existing.priority = priority

    doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
    doc.pipeline_status = pipeline_status
    session.commit()
    publish_job_event(doc_id, stage.value, "done", filename=filename)


def advance_pipeline(doc_id: uuid.UUID) -> None:
    """Determine the next stage(s) and enqueue them.

    Three phases:
      1. Sequential prefix: extract → [ocr] → chunk (linear, same as before)
      2. Fan-out: entities + embed + summarize (all enqueued at once after chunk)
      3. Fan-in: finalize (only when all parallel stages are done)
    """
    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        filename = _doc_filename(doc)

        # Gather completed and in-flight stages
        all_jobs = session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc_id)).scalars().all()
        # Propagate priority from completed jobs
        priority = max((j.priority for j in all_jobs if j.priority), default=0)
        completed = {j.stage for j in all_jobs if j.status == JobStatus.done}
        in_flight = {j.stage for j in all_jobs if j.status in (JobStatus.queued, JobStatus.running)}

        # --- Phase 1: Sequential prefix (extract → ocr → chunk) ---
        for stage in _SEQUENTIAL_STAGES:
            if stage in completed:
                continue
            if stage in in_flight:
                return  # already queued/running, wait

            # Skip OCR if not needed
            if stage == JobStage.ocr and not doc.needs_ocr:
                _mark_skipped(session, doc_id, JobStage.ocr, PipelineStatus.ocr_done, filename, priority=priority)
                completed.add(JobStage.ocr)
                continue

            # Enqueue this sequential stage — close session first since
            # enqueue_stage() creates its own session.
            session.commit()
            session.close()
            enqueue_stage(doc_id, stage, priority=priority)
            return

        # --- Phase 2: Fan-out (entities + embed + summarize) ---
        to_enqueue: list[JobStage] = []
        for stage in _PARALLEL_STAGES:
            if stage in completed or stage in in_flight:
                continue
            # Skip entities if spaCy NER unavailable
            if stage == JobStage.entities and not _is_ner_available():
                _mark_skipped(
                    session,
                    doc_id,
                    JobStage.entities,
                    PipelineStatus.entities_done,
                    filename,
                    reason="spacy_unavailable",
                    priority=priority,
                )
                completed.add(JobStage.entities)
                continue
            to_enqueue.append(stage)

        if to_enqueue:
            session.commit()
            session.close()
            for stage in to_enqueue:
                enqueue_stage(doc_id, stage, priority=priority)
            return

        # --- Phase 3: Fan-in (finalize) ---
        if completed >= _PARALLEL_STAGES and JobStage.finalize not in completed and JobStage.finalize not in in_flight:
            session.commit()
            session.close()
            enqueue_stage(doc_id, JobStage.finalize, priority=priority)
            return

        # Only restore to ready if ALL stages are actually done (not just nothing to enqueue).
        # Phase 2 falls through when something is in_flight but nothing new to enqueue — we
        # must NOT publish finalize-done in that case, it's still processing.
        all_stages_done = (
            JobStage.finalize in completed
            and not in_flight
            and completed >= _PARALLEL_STAGES | {JobStage.extract, JobStage.chunk}
        )
        if all_stages_done and doc.pipeline_status != PipelineStatus.ready:
            doc.pipeline_status = PipelineStatus.ready
            session.commit()
            logger.info("Restored doc %s to ready status", doc_id)
            publish_job_event(doc_id, "finalize", "done", filename=filename)
        else:
            session.commit()

        logger.info("Pipeline advance for doc %s (complete=%s)", doc_id, all_stages_done)
    finally:
        session.close()


def mark_stage_done(
    doc_id: uuid.UUID,
    stage: JobStage,
    *,
    worker_seq: int | None = None,
    **extra_event_fields,
) -> None:
    """Mark a stage as done and advance the pipeline.

    If ``worker_seq`` is provided, verifies the doc's ``pipeline_seq`` still
    matches before marking. If it has been bumped (e.g. by a watcher
    reprocess that fired between the stage's data write and this call),
    silently exits — the new pipeline run will produce fresh results and
    mark its own jobs done. This prevents the worker from falsely marking
    a freshly-queued IngestionJob as done when the original job row was
    deleted by ``_reprocess_doc``.
    """
    done_status = STAGE_DONE_STATUS[stage]

    session = get_sync_session()
    filename = ""
    try:
        # Race check: did pipeline_seq bump while we were running?
        if worker_seq is not None:
            current_seq = session.execute(
                select(Document.pipeline_seq).where(Document.doc_id == doc_id)
            ).scalar_one_or_none()
            if current_seq is None:
                logger.warning(
                    "mark_stage_done: doc %s no longer exists, skipping mark-done for %s",
                    doc_id,
                    stage.value,
                )
                return
            if current_seq != worker_seq:
                logger.info(
                    "mark_stage_done: pipeline_seq bumped during %s for %s "
                    "(worker=%s, current=%s) — skipping mark-done; new pipeline owns it",
                    stage.value,
                    doc_id,
                    worker_seq,
                    current_seq,
                )
                return

        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()
        if job is None:
            # Job row was deleted (e.g. by reprocess between the seq check and now).
            # Exit silently — the new pipeline owns its own job rows.
            logger.info(
                "mark_stage_done: no IngestionJob row for doc=%s stage=%s, skipping",
                doc_id,
                stage.value,
            )
            return
        job.status = JobStatus.done
        job.finished_at = datetime.now(UTC)

        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        doc.pipeline_status = done_status
        filename = _doc_filename(doc)

        session.commit()
    finally:
        session.close()

    publish_job_event(doc_id, stage.value, "done", filename=filename, **extra_event_fields)
    logger.info("Stage %s done for doc %s", stage.value, doc_id)

    # Advance to next stage (unless this was finalize)
    if stage != JobStage.finalize:
        advance_pipeline(doc_id)


def mark_stage_running(
    doc_id: uuid.UUID,
    stage: JobStage,
    *,
    worker_seq: int | None = None,
) -> bool:
    """Check that the job hasn't been cancelled and (optionally) that the
    doc's ``pipeline_seq`` still matches.

    Returns False if the stage should not run:
      - The doc no longer exists
      - ``worker_seq`` was provided and the doc's seq has bumped
      - The IngestionJob row is missing (e.g. deleted by reprocess)
      - The job status is ``error`` (cancelled by user)
    """
    session = get_sync_session()
    filename = None
    try:
        if worker_seq is not None:
            current_seq = session.execute(
                select(Document.pipeline_seq).where(Document.doc_id == doc_id)
            ).scalar_one_or_none()
            if current_seq is None:
                logger.info(
                    "Skipping %s for doc %s — doc no longer exists",
                    stage.value,
                    doc_id,
                )
                return False
            if current_seq != worker_seq:
                logger.info(
                    "Skipping %s for doc %s — pipeline_seq bumped (worker=%s, current=%s)",
                    stage.value,
                    doc_id,
                    worker_seq,
                    current_seq,
                )
                return False

        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()
        if job is None:
            logger.info(
                "Skipping %s for doc %s — IngestionJob row missing (likely deleted by reprocess)",
                stage.value,
                doc_id,
            )
            return False
        if job.status == JobStatus.error:
            logger.info(
                "Skipping %s for doc %s — already cancelled/errored",
                stage.value,
                doc_id,
            )
            return False
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        filename = _doc_filename(doc)
        session.commit()
    finally:
        session.close()

    publish_job_event(doc_id, stage.value, "running", filename=filename)
    return True


def cancel_doc_jobs(doc_id: uuid.UUID) -> int:
    """Cancel all queued/running jobs for a doc, setting them to error."""
    session = get_sync_session()
    cancelled = 0
    cancelled_jobs: list[IngestionJob] = []
    filename = None
    try:
        jobs = (
            session.execute(
                select(IngestionJob).where(
                    IngestionJob.doc_id == doc_id,
                    IngestionJob.status.in_([JobStatus.queued, JobStatus.running]),
                )
            )
            .scalars()
            .all()
        )

        now = datetime.now(UTC)
        for j in jobs:
            j.status = JobStatus.error
            j.error = "Cancelled by user"
            j.finished_at = now
            cancelled_jobs.append(j)
            cancelled += 1

        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc:
            filename = _doc_filename(doc)
            if doc.pipeline_status not in (PipelineStatus.ready, PipelineStatus.error):
                doc.pipeline_status = PipelineStatus.error
                doc.error = "Cancelled by user"

        session.commit()
    finally:
        session.close()

    # Publish SSE events so frontend updates immediately
    for j in cancelled_jobs:
        publish_job_event(
            doc_id,
            j.stage.value,
            "error",
            error="Cancelled by user",
            filename=filename,
        )

    return cancelled
