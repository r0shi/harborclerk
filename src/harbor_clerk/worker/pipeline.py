"""Pipeline orchestrator — enqueue stages, advance pipeline, handle failures."""

import logging
import posixpath
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus

logger = logging.getLogger(__name__)

# Cache NER availability at module level (won't change during worker lifetime)
_ner_available: bool | None = None


def _is_ner_available() -> bool:
    global _ner_available
    if _ner_available is None:
        from harbor_clerk.worker.ner import is_ner_available

        _ner_available = is_ner_available()
    return _ner_available


def _version_filename(version: DocumentVersion) -> str:
    """Extract original filename from the object key (e.g. 'originals/versions/<id>/report.pdf' → 'report.pdf')."""
    return posixpath.basename(version.original_object_key)


# stage → (queue_name, timeout_seconds, version_status_while_running)
STAGE_CONFIG: dict[JobStage, tuple[str, int, VersionStatus]] = {
    JobStage.extract: ("io", 600, VersionStatus.extracting),
    JobStage.ocr: ("cpu", 7200, VersionStatus.ocr_running),
    JobStage.chunk: ("io", 1200, VersionStatus.chunking),
    JobStage.entities: ("io", 900, VersionStatus.extracting_entities),
    JobStage.embed: ("cpu", 1800, VersionStatus.embedding),
    JobStage.summarize: ("io", 900, VersionStatus.summarizing),
    JobStage.finalize: ("io", 600, VersionStatus.finalizing),
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
STAGE_DONE_STATUS: dict[JobStage, VersionStatus] = {
    JobStage.extract: VersionStatus.extracted,
    JobStage.ocr: VersionStatus.ocr_done,
    JobStage.chunk: VersionStatus.chunked,
    JobStage.entities: VersionStatus.entities_done,
    JobStage.embed: VersionStatus.embedded,
    JobStage.summarize: VersionStatus.summarized,
    JobStage.finalize: VersionStatus.ready,
}


def enqueue_stage(version_id: uuid.UUID, stage: JobStage) -> None:
    """Upsert IngestionJob row as queued and notify workers."""
    queue_name, timeout, running_status = STAGE_CONFIG[stage]

    session = get_sync_session()
    try:
        # Upsert ingestion job
        existing = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
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
        else:
            job = IngestionJob(
                version_id=version_id,
                stage=stage,
                status=JobStatus.queued,
            )
            session.add(job)

        # Update version status
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()
        version.status = running_status
        version.error = None

        session.commit()

        filename = _version_filename(version)

        # Notify workers on per-queue channel for instant wakeup
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": f"job_enqueued_{queue_name}", "payload": str(version_id)},
        )
        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "queued", filename=filename)
    logger.info("Enqueued %s for version %s on queue %s", stage.value, version_id, queue_name)


def reset_jobs(version_id: uuid.UUID) -> None:
    """Delete all ingestion jobs for a version so the pipeline starts fresh."""
    session = get_sync_session()
    try:
        jobs = session.execute(select(IngestionJob).where(IngestionJob.version_id == version_id)).scalars().all()
        for j in jobs:
            session.delete(j)
        session.commit()
        logger.info("Reset %d jobs for version %s", len(jobs), version_id)
    finally:
        session.close()


def _mark_skipped(
    session,
    version_id: uuid.UUID,
    stage: JobStage,
    version_status: VersionStatus,
    filename: str,
    *,
    reason: str | None = None,
) -> None:
    """Mark a stage as skipped (done without running) and publish the event."""
    now = datetime.now(UTC)
    metrics: dict = {"skipped": True}
    if reason:
        metrics["reason"] = reason

    existing = session.execute(
        select(IngestionJob).where(
            IngestionJob.version_id == version_id,
            IngestionJob.stage == stage,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            IngestionJob(
                version_id=version_id,
                stage=stage,
                status=JobStatus.done,
                started_at=now,
                finished_at=now,
                metrics=metrics,
            )
        )
    else:
        existing.status = JobStatus.done
        existing.finished_at = now
        existing.metrics = metrics

    version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()
    version.status = version_status
    session.commit()
    publish_job_event(version_id, stage.value, "done", filename=filename)


def advance_pipeline(version_id: uuid.UUID) -> None:
    """Determine the next stage(s) and enqueue them.

    Three phases:
      1. Sequential prefix: extract → [ocr] → chunk (linear, same as before)
      2. Fan-out: entities + embed + summarize (all enqueued at once after chunk)
      3. Fan-in: finalize (only when all parallel stages are done)
    """
    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()
        filename = _version_filename(version)

        # Gather completed and in-flight stages
        all_jobs = session.execute(select(IngestionJob).where(IngestionJob.version_id == version_id)).scalars().all()
        completed = {j.stage for j in all_jobs if j.status == JobStatus.done}
        in_flight = {j.stage for j in all_jobs if j.status in (JobStatus.queued, JobStatus.running)}

        # --- Phase 1: Sequential prefix (extract → ocr → chunk) ---
        for stage in _SEQUENTIAL_STAGES:
            if stage in completed:
                continue
            if stage in in_flight:
                return  # already queued/running, wait

            # Skip OCR if not needed
            if stage == JobStage.ocr and not version.needs_ocr:
                _mark_skipped(session, version_id, JobStage.ocr, VersionStatus.ocr_done, filename)
                completed.add(JobStage.ocr)
                continue

            # Enqueue this sequential stage — close session first since
            # enqueue_stage() creates its own session.
            session.commit()
            session.close()
            enqueue_stage(version_id, stage)
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
                    version_id,
                    JobStage.entities,
                    VersionStatus.entities_done,
                    filename,
                    reason="spacy_unavailable",
                )
                completed.add(JobStage.entities)
                continue
            to_enqueue.append(stage)

        if to_enqueue:
            session.commit()
            session.close()
            for stage in to_enqueue:
                enqueue_stage(version_id, stage)
            return

        # --- Phase 3: Fan-in (finalize) ---
        if completed >= _PARALLEL_STAGES and JobStage.finalize not in completed and JobStage.finalize not in in_flight:
            session.commit()
            session.close()
            enqueue_stage(version_id, JobStage.finalize)
            return

        session.commit()
        logger.info("Pipeline complete for version %s", version_id)
    finally:
        session.close()


def mark_stage_done(version_id: uuid.UUID, stage: JobStage, **extra_event_fields) -> None:
    """Mark a stage as done and advance the pipeline."""
    done_status = STAGE_DONE_STATUS[stage]

    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one()
        job.status = JobStatus.done
        job.finished_at = datetime.now(UTC)

        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()
        version.status = done_status
        filename = _version_filename(version)

        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "done", filename=filename, **extra_event_fields)
    logger.info("Stage %s done for version %s", stage.value, version_id)

    # Advance to next stage (unless this was finalize)
    if stage != JobStage.finalize:
        advance_pipeline(version_id)


def mark_stage_running(version_id: uuid.UUID, stage: JobStage) -> bool:
    """Check that job hasn't been cancelled. Returns False if errored/cancelled."""
    session = get_sync_session()
    filename = None
    try:
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one()
        if job.status == JobStatus.error:
            logger.info(
                "Skipping %s for version %s — already cancelled/errored",
                stage.value,
                version_id,
            )
            return False
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()
        filename = _version_filename(version)
        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "running", filename=filename)
    return True


def cancel_version_jobs(version_id: uuid.UUID) -> int:
    """Cancel all queued/running jobs for a version, setting them to error."""
    session = get_sync_session()
    cancelled = 0
    cancelled_jobs: list[IngestionJob] = []
    filename = None
    try:
        jobs = (
            session.execute(
                select(IngestionJob).where(
                    IngestionJob.version_id == version_id,
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

        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one_or_none()
        if version:
            filename = _version_filename(version)
            if version.status not in (VersionStatus.ready, VersionStatus.error):
                version.status = VersionStatus.error
                version.error = "Cancelled by user"

        session.commit()
    finally:
        session.close()

    # Publish SSE events so frontend updates immediately
    for j in cancelled_jobs:
        publish_job_event(
            version_id,
            j.stage.value,
            "error",
            error="Cancelled by user",
            filename=filename,
        )

    return cancelled
