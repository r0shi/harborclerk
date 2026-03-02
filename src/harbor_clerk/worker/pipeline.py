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

# Ordered pipeline stages
STAGE_ORDER = [
    JobStage.extract,
    JobStage.ocr,
    JobStage.chunk,
    JobStage.entities,
    JobStage.embed,
    JobStage.summarize,
    JobStage.finalize,
]

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


def advance_pipeline(version_id: uuid.UUID) -> None:
    """Determine the next stage and enqueue it, skipping OCR if not needed."""
    session = get_sync_session()
    try:
        version = session.execute(select(DocumentVersion).where(DocumentVersion.version_id == version_id)).scalar_one()

        # Find the last completed stage
        completed_stages = set()
        jobs = (
            session.execute(
                select(IngestionJob).where(
                    IngestionJob.version_id == version_id,
                    IngestionJob.status == JobStatus.done,
                )
            )
            .scalars()
            .all()
        )
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
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        metrics={"skipped": True},
                    )
                    session.add(skipped_job)
                else:
                    existing.status = JobStatus.done
                    existing.finished_at = datetime.now(UTC)
                    existing.metrics = {"skipped": True}
                version.status = VersionStatus.ocr_done
                session.commit()
                publish_job_event(version_id, "ocr", "done", filename=_version_filename(version))
                continue

            # Skip entities stage if spaCy NER is not available
            if stage == JobStage.entities and not _is_ner_available():
                existing = session.execute(
                    select(IngestionJob).where(
                        IngestionJob.version_id == version_id,
                        IngestionJob.stage == JobStage.entities,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    skipped_job = IngestionJob(
                        version_id=version_id,
                        stage=JobStage.entities,
                        status=JobStatus.done,
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        metrics={"skipped": True, "reason": "spacy_unavailable"},
                    )
                    session.add(skipped_job)
                else:
                    existing.status = JobStatus.done
                    existing.finished_at = datetime.now(UTC)
                    existing.metrics = {"skipped": True, "reason": "spacy_unavailable"}
                version.status = VersionStatus.entities_done
                session.commit()
                publish_job_event(version_id, "entities", "done", filename=_version_filename(version))
                continue

            # Commit skip changes (if any) before enqueuing next stage,
            # which creates its own session.
            session.commit()
            session.close()
            enqueue_stage(version_id, stage)
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
