"""PostgreSQL-polling worker entry point with LISTEN wakeup and heartbeat.

Usage:
    harbor-clerk-worker                    # reads RQ_QUEUES env var (default: io)
    harbor-clerk-worker --queues io cpu    # explicit queue names
"""

import argparse
import logging
import os
import select as select_mod
import signal
import threading
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extensions
from sqlalchemy import select, update

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import _make_sync_url, get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import DocumentVersion, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
from harbor_clerk.worker.pipeline import STAGE_CONFIG
from harbor_clerk.worker.stages import STAGE_FUNCTIONS

logger = logging.getLogger(__name__)

QUEUE_STAGES: dict[str, list[JobStage]] = {
    "io": [
        JobStage.extract,
        JobStage.chunk,
        JobStage.entities,
        JobStage.summarize,
        JobStage.finalize,
    ],
    "cpu": [JobStage.ocr, JobStage.embed],
}

HEARTBEAT_INTERVAL = 30  # seconds

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def _get_listen_connection():
    """Get a raw psycopg2 connection for LISTEN (outside SQLAlchemy pool)."""
    settings = get_settings()
    dsn = _make_sync_url(settings.database_url).replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _wait_for_notify(conn, timeout=30):
    """Block until a NOTIFY arrives or timeout expires."""
    if select_mod.select([conn], [], [], timeout) != ([], [], []):
        conn.poll()
        while conn.notifies:
            conn.notifies.pop(0)


def _heartbeat_loop(
    version_id: uuid.UUID, stage: JobStage, stop_event: threading.Event
):
    """Update heartbeat_at every HEARTBEAT_INTERVAL seconds until stop_event is set."""
    while not stop_event.wait(timeout=HEARTBEAT_INTERVAL):
        session = get_sync_session()
        try:
            session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.version_id == version_id,
                    IngestionJob.stage == stage,
                )
                .values(heartbeat_at=datetime.now(timezone.utc))
            )
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()


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

        now = datetime.now(timezone.utc)
        row.status = JobStatus.running
        row.started_at = now
        row.heartbeat_at = now
        version_id = row.version_id
        stage = row.stage
        session.commit()
        return (version_id, stage)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _lookup_filename(version_id: uuid.UUID) -> str | None:
    """Look up the original filename for a version."""
    import posixpath

    session = get_sync_session()
    try:
        version = session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == version_id)
        ).scalar_one_or_none()
        if version:
            return posixpath.basename(version.original_object_key)
        return None
    finally:
        session.close()


def execute_job(version_id: uuid.UUID, stage: JobStage) -> None:
    """Run a stage function with timeout enforcement via signal.alarm() and heartbeat."""
    _, timeout, _ = STAGE_CONFIG[stage]
    func = STAGE_FUNCTIONS[stage]
    filename = _lookup_filename(version_id)

    logger.info(
        "Starting %s for version %s (timeout=%ds)", stage.value, version_id, timeout
    )
    publish_job_event(version_id, stage.value, "running", filename=filename)

    # Start heartbeat thread
    stop_heartbeat = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(version_id, stage, stop_heartbeat),
        daemon=True,
    )
    hb_thread.start()

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

        publish_job_event(
            version_id, stage.value, "error", error=error_msg, filename=filename
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        stop_heartbeat.set()
        hb_thread.join(timeout=5)


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

    # Set up graceful shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Set up LISTEN connection for instant wakeup
    listen_conn = _get_listen_connection()
    try:
        with listen_conn.cursor() as cur:
            for q in args.queues:
                cur.execute(f"LISTEN job_enqueued_{q}")

        while not _shutdown:
            result = claim_next_job(stages)
            if result:
                execute_job(*result)
            else:
                _wait_for_notify(listen_conn, timeout=30)
    finally:
        listen_conn.close()
        logger.info("Worker shut down")


if __name__ == "__main__":
    main()
