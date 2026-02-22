"""Pipeline orchestrator — enqueue stages, advance pipeline, handle failures."""

import logging
import uuid
from datetime import datetime, timezone

from redis import Redis
from rq import Queue
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus

logger = logging.getLogger(__name__)

# stage → (queue_name, timeout_seconds, version_status_while_running)
STAGE_CONFIG: dict[JobStage, tuple[str, int, VersionStatus]] = {
    JobStage.extract:  ("io",  600,  VersionStatus.extracting),
    JobStage.ocr:      ("cpu", 7200, VersionStatus.ocr_running),
    JobStage.chunk:    ("io",  1200, VersionStatus.chunking),
    JobStage.embed:    ("cpu", 1800, VersionStatus.embedding),
    JobStage.finalize: ("io",  600,  VersionStatus.ready),
}

# Ordered pipeline stages
STAGE_ORDER = [
    JobStage.extract,
    JobStage.ocr,
    JobStage.chunk,
    JobStage.embed,
    JobStage.finalize,
]

# Status after stage completes
STAGE_DONE_STATUS: dict[JobStage, VersionStatus] = {
    JobStage.extract:  VersionStatus.extracted,
    JobStage.ocr:      VersionStatus.ocr_done,
    JobStage.chunk:    VersionStatus.chunked,
    JobStage.embed:    VersionStatus.embedded,
    JobStage.finalize: VersionStatus.ready,
}


def _get_rq_queue(queue_name: str) -> Queue:
    settings = get_settings()
    conn = Redis.from_url(settings.redis_url)
    return Queue(queue_name, connection=conn)


def enqueue_stage(version_id: uuid.UUID, stage: JobStage) -> None:
    """Upsert IngestionJob row and enqueue the RQ job."""
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
        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one()
        version.status = running_status
        version.error = None

        session.commit()
    finally:
        session.close()

    # Enqueue RQ job
    q = _get_rq_queue(queue_name)
    from harbor_clerk.worker.stages import STAGE_FUNCTIONS
    func = STAGE_FUNCTIONS[stage]
    q.enqueue(
        func,
        version_id,
        job_timeout=timeout,
        on_failure=on_job_failure,
        result_ttl=0,
    )

    publish_job_event(version_id, stage.value, "queued")
    logger.info("Enqueued %s for version %s on queue %s", stage.value, version_id, queue_name)


def advance_pipeline(version_id: uuid.UUID) -> None:
    """Determine the next stage and enqueue it, skipping OCR if not needed."""
    session = get_sync_session()
    try:
        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one()

        # Find the last completed stage
        completed_stages = set()
        jobs = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.status == JobStatus.done,
            )
        ).scalars().all()
        for j in jobs:
            completed_stages.add(j.stage)

        # Find next stage
        for stage in STAGE_ORDER:
            if stage in completed_stages:
                continue
            # Skip OCR if not needed
            if stage == JobStage.ocr and not version.needs_ocr:
                # Mark OCR as done without running
                existing = session.execute(
                    select(IngestionJob).where(
                        IngestionJob.version_id == version_id,
                        IngestionJob.stage == JobStage.ocr,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    skipped_job = IngestionJob(
                        version_id=version_id,
                        stage=JobStage.ocr,
                        status=JobStatus.done,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                        metrics={"skipped": True},
                    )
                    session.add(skipped_job)
                else:
                    existing.status = JobStatus.done
                    existing.finished_at = datetime.now(timezone.utc)
                    existing.metrics = {"skipped": True}
                version.status = VersionStatus.ocr_done
                session.commit()
                publish_job_event(version_id, "ocr", "done")
                continue

            session.close()
            enqueue_stage(version_id, stage)
            return

        logger.info("Pipeline complete for version %s", version_id)
    finally:
        session.close()


def mark_stage_done(version_id: uuid.UUID, stage: JobStage) -> None:
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
        job.finished_at = datetime.now(timezone.utc)

        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one()
        version.status = done_status

        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "done")
    logger.info("Stage %s done for version %s", stage.value, version_id)

    # Advance to next stage (unless this was finalize)
    if stage != JobStage.finalize:
        advance_pipeline(version_id)


def mark_stage_running(version_id: uuid.UUID, stage: JobStage) -> None:
    """Mark a stage as running."""
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one()
        job.status = JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "running")


def on_job_failure(job, connection, type, value, traceback):
    """RQ on_failure callback — marks job + version as error."""
    error_msg = f"{type.__name__}: {value}" if type and value else "Unknown error"
    logger.error("Job %s failed: %s", job.id, error_msg)

    # Extract version_id and stage from the job args
    if not job.args:
        return
    version_id = job.args[0]
    func_name = job.func_name or ""

    # Determine stage from function name
    stage = None
    for s in JobStage:
        if s.value in func_name:
            stage = s
            break

    if stage is None:
        return

    session = get_sync_session()
    try:
        ing_job = session.execute(
            select(IngestionJob).where(
                IngestionJob.version_id == version_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()
        if ing_job:
            ing_job.status = JobStatus.error
            ing_job.error = error_msg
            ing_job.finished_at = datetime.now(timezone.utc)

        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one_or_none()
        if version:
            version.status = VersionStatus.error
            version.error = error_msg

        session.commit()
    finally:
        session.close()

    publish_job_event(version_id, stage.value, "error", error=error_msg)
