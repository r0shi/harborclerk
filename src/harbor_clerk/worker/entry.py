"""PostgreSQL-polling worker entry point.

Usage:
    harbor-clerk-worker                    # reads RQ_QUEUES env var (default: io)
    harbor-clerk-worker --queues io cpu    # explicit queue names
"""

import argparse
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
from harbor_clerk.worker.pipeline import STAGE_CONFIG
from harbor_clerk.worker.stages import STAGE_FUNCTIONS

logger = logging.getLogger(__name__)

QUEUE_STAGES: dict[str, list[JobStage]] = {
    "io":  [JobStage.extract, JobStage.chunk, JobStage.finalize],
    "cpu": [JobStage.ocr, JobStage.embed],
}

POLL_INTERVAL = 2  # seconds


def claim_next_job(stages: list[JobStage]) -> tuple[uuid.UUID, JobStage] | None:
    """Atomically claim the next queued job using SELECT ... FOR UPDATE SKIP LOCKED."""
    session = get_sync_session()
    try:
        row = session.execute(
            select(IngestionJob)
            .where(
                IngestionJob.status == JobStatus.queued,
                IngestionJob.stage.in_(stages),
            )
            .order_by(IngestionJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if row is None:
            return None

        row.status = JobStatus.running
        row.started_at = datetime.now(timezone.utc)
        version_id = row.version_id
        stage = row.stage
        session.commit()
        return (version_id, stage)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def execute_job(version_id: uuid.UUID, stage: JobStage) -> None:
    """Run a stage function with timeout enforcement via signal.alarm()."""
    _, timeout, _ = STAGE_CONFIG[stage]
    func = STAGE_FUNCTIONS[stage]

    logger.info("Starting %s for version %s (timeout=%ds)", stage.value, version_id, timeout)
    publish_job_event(version_id, stage.value, "running")

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Stage {stage.value} timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)
    try:
        func(version_id)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.error("Job %s/%s failed: %s", version_id, stage.value, error_msg)

        session = get_sync_session()
        try:
            job = session.execute(
                select(IngestionJob).where(
                    IngestionJob.version_id == version_id,
                    IngestionJob.stage == stage,
                )
            ).scalar_one_or_none()
            if job:
                job.status = JobStatus.error
                job.error = error_msg
                job.finished_at = datetime.now(timezone.utc)

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
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def main():
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Harbor Clerk worker")
    parser.add_argument(
        "--queues",
        nargs="+",
        default=os.environ.get("RQ_QUEUES", "io").split(","),
        help="Queue names to listen on",
    )
    args = parser.parse_args()

    stages: list[JobStage] = []
    for q in args.queues:
        stages.extend(QUEUE_STAGES[q])

    logger.info("Worker starting, listening for stages: %s", [s.value for s in stages])

    while True:
        result = claim_next_job(stages)
        if result:
            execute_job(*result)
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
