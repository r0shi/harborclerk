"""PostgreSQL-polling worker entry point with LISTEN wakeup and heartbeat.

Usage:
    harbor-clerk-worker                    # reads RQ_QUEUES env var (default: io)
    harbor-clerk-worker --queues io cpu    # explicit queue names
"""

import argparse
import logging
import math
import os
import select as select_mod
import signal
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import psycopg2
import psycopg2.extensions
from sqlalchemy import and_, case, or_, select, update

from harbor_clerk.config import get_settings, refresh_llm_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.db_health import panic_on_sentinel_mismatch
from harbor_clerk.db_sync import _make_sync_url, get_sync_session
from harbor_clerk.events import publish_job_event
from harbor_clerk.llm.summarize import AppleIntelligenceUnavailableError
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.worker.pipeline import _BACKGROUND_STAGES, STAGE_CONFIG
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
AFM_RETRY_DELAYS_SECONDS = (1, 5, 10, 30, 60, 120, 300, 600)
AFM_RETRY_MAX_ATTEMPTS = len(AFM_RETRY_DELAYS_SECONDS) + 1
AFM_RETRY_REASON = "apple_intelligence_unavailable"

_shutdown = False


class _AfmUnavailableJobResult(NamedTuple):
    disposition: str
    attempts: int
    delay_seconds: int | None
    retry_after: datetime | None


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


def _afm_retry_delay_seconds(attempt: int) -> int:
    """Exponential retry delay for forced-AFM summarize outages."""
    bounded_attempt = max(1, attempt)
    index = min(bounded_attempt - 1, len(AFM_RETRY_DELAYS_SECONDS) - 1)
    return AFM_RETRY_DELAYS_SECONDS[index]


def _summarize_afm_claim_state(stages: list[JobStage]) -> tuple[bool, tuple[float, str] | None]:
    """Return whether forced AFM is active and any current cooldown."""
    if JobStage.summarize not in stages:
        return False, None
    refresh_llm_settings()
    if not get_settings().summary_force_apple_intelligence:
        return False, None
    from harbor_clerk.llm.summarize import apple_intelligence_cooldown

    return True, apple_intelligence_cooldown()


def _parse_retry_after(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _next_afm_retry_wait_seconds(stages: list[JobStage], *, default: int = 30) -> int:
    """Return an idle wait that wakes when the next forced-AFM retry is due."""
    forced_afm_summaries, _ = _summarize_afm_claim_state(stages)
    if not forced_afm_summaries:
        return default

    session = get_sync_session()
    try:
        retry_after = session.execute(
            select(IngestionJob.metrics["retry_after"].astext)
            .join(Document, Document.doc_id == IngestionJob.doc_id)
            .where(
                Document.status == "active",
                IngestionJob.stage == JobStage.summarize,
                IngestionJob.status == JobStatus.queued,
                IngestionJob.pipeline_seq == Document.pipeline_seq,
                IngestionJob.metrics["reason"].astext == AFM_RETRY_REASON,
            )
            .order_by(IngestionJob.metrics["retry_after"].astext.asc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()

    parsed = _parse_retry_after(retry_after)
    if parsed is None:
        return default
    remaining = (parsed - datetime.now(UTC)).total_seconds()
    return max(1, min(default, math.ceil(remaining)))


def _heartbeat_loop(doc_id: uuid.UUID, stage: JobStage, pipeline_seq: int, stop_event: threading.Event):
    """Update heartbeat_at every HEARTBEAT_INTERVAL seconds until stop_event is set."""
    while not stop_event.wait(timeout=HEARTBEAT_INTERVAL):
        session = get_sync_session()
        try:
            session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.doc_id == doc_id,
                    IngestionJob.stage == stage,
                    IngestionJob.pipeline_seq == pipeline_seq,
                )
                .values(heartbeat_at=datetime.now(UTC))
            )
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()


def claim_next_job(stages: list[JobStage]) -> tuple[uuid.UUID, JobStage, int] | None:
    """Atomically claim the next queued job using SELECT ... FOR UPDATE SKIP LOCKED."""
    stages_to_claim = list(stages)
    forced_afm_summaries, afm_cooldown = _summarize_afm_claim_state(stages_to_claim)
    if afm_cooldown is not None:
        remaining, reason = afm_cooldown
        stages_to_claim = [stage for stage in stages_to_claim if stage != JobStage.summarize]
        logger.info(
            "Skipping summarize claims while Apple Intelligence is cooling down (%.0fs remaining: %s)",
            remaining,
            reason,
        )
        if not stages_to_claim:
            return None

    now_iso = datetime.now(UTC).isoformat()
    filters = [
        IngestionJob.status == JobStatus.queued,
        IngestionJob.stage.in_(stages_to_claim),
        IngestionJob.pipeline_seq == Document.pipeline_seq,
        Document.status == "active",
    ]

    session = get_sync_session()
    try:
        afm_retry_pending = False
        if forced_afm_summaries and JobStage.summarize in stages_to_claim:
            afm_retry_pending = (
                session.execute(
                    select(IngestionJob.job_id)
                    .join(Document, Document.doc_id == IngestionJob.doc_id)
                    .where(
                        Document.status == "active",
                        IngestionJob.stage == JobStage.summarize,
                        IngestionJob.status == JobStatus.queued,
                        IngestionJob.pipeline_seq == Document.pipeline_seq,
                        IngestionJob.metrics["reason"].astext == AFM_RETRY_REASON,
                    )
                    .limit(1)
                ).first()
                is not None
            )
            if afm_retry_pending:
                filters.append(
                    or_(
                        IngestionJob.stage != JobStage.summarize,
                        and_(
                            IngestionJob.metrics["reason"].astext == AFM_RETRY_REASON,
                            or_(
                                IngestionJob.metrics["retry_after"].astext.is_(None),
                                IngestionJob.metrics["retry_after"].astext <= now_iso,
                            ),
                        ),
                    )
                )
            else:
                filters.append(
                    or_(
                        IngestionJob.stage != JobStage.summarize,
                        IngestionJob.metrics["retry_after"].astext.is_(None),
                        IngestionJob.metrics["retry_after"].astext <= now_iso,
                    )
                )

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
        retry_probe_priority = case(
            (
                and_(
                    IngestionJob.stage == JobStage.summarize,
                    IngestionJob.metrics["reason"].astext == AFM_RETRY_REASON,
                ),
                0,
            ),
            else_=1,
        )
        row = session.execute(
            select(IngestionJob)
            .join(Document, Document.doc_id == IngestionJob.doc_id)
            .where(*filters)
            .order_by(IngestionJob.priority, retry_probe_priority, stage_priority, IngestionJob.created_at)
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
        pipeline_seq = row.pipeline_seq
        session.commit()
        return (doc_id, stage, pipeline_seq)
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
    finally:
        session.close()


def _record_afm_unavailable_job(
    doc_id: uuid.UUID,
    stage: JobStage,
    pipeline_seq: int,
    error_msg: str,
) -> _AfmUnavailableJobResult | None:
    if stage != JobStage.summarize:
        return None

    now = datetime.now(UTC)
    session = get_sync_session()
    try:
        job = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()
        if job is None or job.pipeline_seq != pipeline_seq:
            return None

        metrics = dict(job.metrics or {})
        attempts = int(metrics.get("retry_attempts") or 0) + 1
        metrics.update(
            {
                "reason": AFM_RETRY_REASON,
                "retry_attempts": attempts,
                "last_error": error_msg,
                "last_failed_at": now.isoformat(),
            }
        )
        if attempts >= AFM_RETRY_MAX_ATTEMPTS:
            metrics["blocked"] = False
            metrics["retry_exhausted"] = True
            metrics.pop("retry_after", None)
            job.status = JobStatus.error
            job.error = error_msg
            job.metrics = metrics
            job.finished_at = now
            job.heartbeat_at = None
            session.commit()
            return _AfmUnavailableJobResult("error", attempts, None, None)

        delay_seconds = _afm_retry_delay_seconds(attempts)
        retry_after = now + timedelta(seconds=delay_seconds)
        metrics.update(
            {
                "blocked": True,
                "retry_after": retry_after.isoformat(),
            }
        )

        job.status = JobStatus.queued
        job.error = None
        job.metrics = metrics
        job.started_at = None
        job.finished_at = None
        job.heartbeat_at = None
        job.progress_current = 0
        job.progress_total = 0
        session.commit()
        return _AfmUnavailableJobResult("retry", attempts, delay_seconds, retry_after)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def execute_job(doc_id: uuid.UUID, stage: JobStage, pipeline_seq: int | None = None) -> None:
    """Run a stage function with timeout enforcement via signal.alarm() and heartbeat."""
    _, timeout, _ = STAGE_CONFIG[stage]
    func = STAGE_FUNCTIONS[stage]
    filename = _lookup_filename(doc_id)
    if pipeline_seq is None:
        session = get_sync_session()
        try:
            pipeline_seq = session.execute(select(Document.pipeline_seq).where(Document.doc_id == doc_id)).scalar()
            pipeline_seq = pipeline_seq or 0
        finally:
            session.close()

    logger.info("Starting %s for doc %s (timeout=%ds)", stage.value, doc_id, timeout)
    publish_job_event(doc_id, stage.value, "running", filename=filename)

    # Start heartbeat thread
    stop_heartbeat = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(doc_id, stage, pipeline_seq, stop_heartbeat),
        daemon=True,
    )
    hb_thread.start()

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Stage {stage.value} timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)
    try:
        func(doc_id, worker_seq=pipeline_seq)
    except AppleIntelligenceUnavailableError as e:
        error_msg = f"{type(e).__name__}: {e}"
        disposition = _record_afm_unavailable_job(doc_id, stage, pipeline_seq, error_msg)
        if disposition is None:
            logger.error("Job %s/%s failed: %s", doc_id, stage.value, error_msg)
            publish_job_event(doc_id, stage.value, "error", error=error_msg, filename=filename)
        elif disposition.disposition == "error":
            logger.error(
                "Job %s/%s failed after %d Apple Intelligence retry attempts: %s",
                doc_id,
                stage.value,
                disposition.attempts,
                error_msg,
            )
            publish_job_event(doc_id, stage.value, "error", error=error_msg, filename=filename)
        else:
            assert disposition.delay_seconds is not None
            assert disposition.retry_after is not None
            logger.warning(
                "Job %s/%s blocked by Apple Intelligence; retry attempt %d/%d in %ds at %s",
                doc_id,
                stage.value,
                disposition.attempts,
                AFM_RETRY_MAX_ATTEMPTS,
                disposition.delay_seconds,
                disposition.retry_after.isoformat(),
            )
            publish_job_event(
                doc_id,
                stage.value,
                "queued",
                filename=filename,
                error=error_msg,
                retry_after=disposition.retry_after.isoformat(),
            )
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
            if job and job.pipeline_seq == pipeline_seq:
                job.status = JobStatus.error
                job.error = error_msg
                job.finished_at = datetime.now(UTC)

            # Background stages (summarize) must not touch pipeline_status —
            # it reflects the gating-stage progression only. The doc may
            # already be `ready` when summarize errors; flipping it to
            # `error` would regress a successfully-ingested doc into a
            # failed-looking state in the UI just because the optional
            # summary couldn't be produced. enqueue_stage + mark_stage_done
            # already enforce this for the success path; the error path
            # missed the memo when PR #327 moved summarize to background.
            doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
            if doc and doc.pipeline_seq == pipeline_seq and stage not in _BACKGROUND_STAGES:
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


def _check_sentinel() -> None:
    """Run verify_schema_sentinel synchronously; on mismatch log CRITICAL and exit(2).

    This is a thin sync wrapper around ``panic_on_sentinel_mismatch`` so that
    the synchronous ``main()`` entry point can call it without requiring a
    pre-existing event loop.  Having it as a named module-level function also
    makes it easily patchable in tests.
    """
    import asyncio

    async def _inner() -> None:
        async with async_session_factory() as db:
            await panic_on_sentinel_mismatch(db)

    asyncio.run(_inner())


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

    # Set up the unified Tesseract data dir so per-language pack
    # .traineddata files are findable alongside bundled ones. Idempotent
    # — re-running on worker restart picks up packs installed since the
    # previous launch. Only relevant for the cpu queue (which runs OCR);
    # cheap enough to do unconditionally.
    try:
        from harbor_clerk.lang_packs.tesseract_setup import setup_unified_tessdata_dir

        setup_unified_tessdata_dir()
    except Exception:
        # Don't block worker startup on lang-pack setup failures — OCR
        # will fall back to whatever TESSDATA_PREFIX was originally set
        # to (bundled English-only).
        logger.exception("Failed to set up unified tessdata dir; OCR may not see language packs")

    stages: list[JobStage] = []
    for q in args.queues:
        stages.extend(QUEUE_STAGES[q])

    logger.info("Worker starting, listening for stages: %s", [s.value for s in stages])

    # Refuse to start if the DB schema doesn't match this binary's embedding config.
    _check_sentinel()

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
                _wait_for_notify(listen_conn, timeout=_next_afm_retry_wait_seconds(stages))
    finally:
        listen_conn.close()
        logger.info("Worker shut down")


if __name__ == "__main__":
    main()
