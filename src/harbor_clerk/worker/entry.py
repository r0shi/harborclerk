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
from datetime import UTC, datetime

import psycopg2
import psycopg2.extensions
from sqlalchemy import case, select, update

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import _make_sync_url, get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import STAGE_CONFIG
from harbor_clerk.worker.stages import STAGE_FUNCTIONS

logger = logging.getLogger(__name__)

QUEUE_STAGES: dict[str, list[JobStage]] = {
    "io": [
        JobStage.extract,
        JobStage.chunk,
        JobStage.entities,
        JobStage.finalize,
    ],
    "cpu": [JobStage.ocr, JobStage.embed],
    # `summarize` calls llama-server, which has a single parallel slot
    # (-np 1) on the heavy 22 GB+ models we target. Putting it on its
    # own queue with a dedicated single-worker process means at most one
    # summarize job ever runs at a time, so the IO/CPU pools don't get
    # tied up busy-waiting on the LLM lock and other work (extract,
    # chunk, finalize, OCR, embed) keeps flowing.
    "llm": [JobStage.summarize],
}

HEARTBEAT_INTERVAL = 30  # seconds

_shutdown = False


def _resolve_tmpdir_symlinks() -> None:
    """Set TMPDIR to the realpath of the system temp directory.

    On macOS, the default `/tmp` is a symlink to `/private/tmp`. The bundled
    Homebrew-built tesseract (via Leptonica) cannot open files passed as
    `/tmp/<file>` — it falls back to interpreting the input file's first line
    as a list of filenames, which for a PNG means treating the magic bytes
    `\\x89PNG\\r\\n\\x1a\\n` as a path. The resulting Leptonica error is
    written to stderr with the binary `\\x89` byte still in it, and pytesseract's
    `get_errors()` blows up trying to UTF-8-decode that stderr — surfacing as
    `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x89 in position N`.

    Replacing TMPDIR with `/private/tmp` (the realpath) sidesteps the symlink
    issue: pytesseract writes its temp PNG to a path tesseract can actually open.

    No-op on Linux (`/tmp` isn't a symlink) and on macOS user sessions where
    TMPDIR is already set to `/var/folders/.../T/`. Only matters when the
    worker is launched from a context where TMPDIR is unset and Python's
    tempfile module falls back to `/tmp`.

    Two layers of belt-and-suspenders:

    1. ``os.environ["TMPDIR"]`` — durable; inherited by every subprocess
       and read by every C extension that consults the standard env var.
       This is the primary fix.
    2. ``tempfile.tempdir`` — the in-process cache used by pure-Python
       ``NamedTemporaryFile`` etc. Mutable module-level state that some
       libraries (notably ``pytest`` itself, certain ``click``-based tools,
       and rare cases in ``multiprocessing``) reset to ``None`` to force a
       re-detection. If that happens mid-process, ``tempfile`` will re-read
       TMPDIR from the env var (which we set in step 1), so the fix is
       resilient. The redundancy here is intentional — assigning both
       guarantees the very next ``NamedTemporaryFile`` call lands on the
       resolved path without depending on internal cache invalidation.
    """
    import tempfile

    current = tempfile.gettempdir()
    resolved = os.path.realpath(current)
    if current != resolved:
        os.environ["TMPDIR"] = resolved + os.sep
        # Re-init tempfile's cached temp dir so subsequent NamedTemporaryFile
        # calls (including pytesseract's) see the new TMPDIR. See the docstring
        # above for why we set both env var AND module attribute.
        tempfile.tempdir = resolved
        logger.info("Resolved TMPDIR symlink: %s -> %s", current, resolved)


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def _get_listen_connection():
    """Get a raw psycopg2 connection for LISTEN (outside SQLAlchemy pool)."""
    settings = get_settings()
    dsn = _make_sync_url(settings.database_url).replace("postgresql+psycopg2://", "postgresql://")
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _wait_for_notify(conn, timeout=30):
    """Block until a NOTIFY arrives or timeout expires."""
    if select_mod.select([conn], [], [], timeout) != ([], [], []):
        conn.poll()
        while conn.notifies:
            conn.notifies.pop(0)


def _heartbeat_loop(doc_id: uuid.UUID, stage: JobStage, stop_event: threading.Event):
    """Update heartbeat_at every HEARTBEAT_INTERVAL seconds until stop_event is set."""
    while not stop_event.wait(timeout=HEARTBEAT_INTERVAL):
        session = get_sync_session()
        try:
            session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.doc_id == doc_id,
                    IngestionJob.stage == stage,
                )
                .values(heartbeat_at=datetime.now(UTC))
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
        # Prioritise stages closest to completion so fast final stages
        # (finalize, entities) aren't starved by slow ones (summarize).
        stage_priority = case(
            (IngestionJob.stage == JobStage.finalize, 0),
            (IngestionJob.stage == JobStage.entities, 1),
            (IngestionJob.stage == JobStage.extract, 2),
            (IngestionJob.stage == JobStage.chunk, 3),
            (IngestionJob.stage == JobStage.summarize, 4),
            else_=5,
        )
        row = session.execute(
            select(IngestionJob)
            .where(
                IngestionJob.status == JobStatus.queued,
                IngestionJob.stage.in_(stages),
            )
            .order_by(IngestionJob.priority, stage_priority, IngestionJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if row is None:
            return None

        now = datetime.now(UTC)
        row.status = JobStatus.running
        row.started_at = now
        row.heartbeat_at = now
        doc_id = row.doc_id
        stage = row.stage
        session.commit()
        return (doc_id, stage)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _lookup_filename(doc_id: uuid.UUID) -> str | None:
    """Look up the original filename for a document."""
    import posixpath

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc:
            if doc.original_object_key:
                return posixpath.basename(doc.original_object_key)
            if doc.source_path:
                return posixpath.basename(doc.source_path)
            return "unknown"
        return None
    finally:
        session.close()


def execute_job(doc_id: uuid.UUID, stage: JobStage) -> None:
    """Run a stage function with timeout enforcement via signal.alarm() and heartbeat."""
    _, timeout, _ = STAGE_CONFIG[stage]
    func = STAGE_FUNCTIONS[stage]
    filename = _lookup_filename(doc_id)

    logger.info("Starting %s for doc %s (timeout=%ds)", stage.value, doc_id, timeout)
    publish_job_event(doc_id, stage.value, "running", filename=filename)

    # Start heartbeat thread
    stop_heartbeat = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(doc_id, stage, stop_heartbeat),
        daemon=True,
    )
    hb_thread.start()

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Stage {stage.value} timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)
    try:
        func(doc_id)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.error("Job %s/%s failed: %s", doc_id, stage.value, error_msg)

        session = get_sync_session()
        try:
            job = session.execute(
                select(IngestionJob).where(
                    IngestionJob.doc_id == doc_id,
                    IngestionJob.stage == stage,
                )
            ).scalar_one_or_none()
            if job:
                job.status = JobStatus.error
                job.error = error_msg
                job.finished_at = datetime.now(UTC)

            doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
            if doc:
                doc.pipeline_status = PipelineStatus.error
                doc.error = error_msg

            session.commit()
        finally:
            session.close()

        publish_job_event(doc_id, stage.value, "error", error=error_msg, filename=filename)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        stop_heartbeat.set()
        hb_thread.join(timeout=5)


def main():
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Harbor Clerk worker")
    parser.add_argument(
        "--queues",
        nargs="+",
        default=os.environ.get("RQ_QUEUES", "io").split(","),
        help="Queue names to listen on",
    )
    args = parser.parse_args()

    from harbor_clerk.log_setup import setup_logging

    queue_suffix = "-".join(args.queues)
    setup_logging(f"worker-{queue_suffix}", settings.log_level)

    _resolve_tmpdir_symlinks()

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
