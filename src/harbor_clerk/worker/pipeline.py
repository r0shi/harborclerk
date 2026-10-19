"""Pipeline orchestrator — enqueue stages, advance pipeline, handle failures."""

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

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


def _doc_filename(doc: Document, session=None) -> str:
    """The document's filename for job events (``watched_source.display_filename``)."""
    from harbor_clerk.watched_source import display_filename

    return display_filename(session, doc)


def check_pipeline_seq(session, doc_id: uuid.UUID, expected_seq: int) -> bool:
    """Return True if the doc's pipeline_seq still matches.

    The seq alone answers only "has the content changed?". A stage's race
    check before writing its results must not use it: a cancel, or a cancel
    then Retry and a newer claim, leave the seq unchanged. Stages ask
    ``stage_run_is_current`` instead, which checks the seq and those too.
    """
    current = session.execute(select(Document.pipeline_seq).where(Document.doc_id == doc_id)).scalar()
    return current == expected_seq


def stage_run_is_current(session, doc_id: uuid.UUID, stage: JobStage, worker_seq: int) -> bool:
    """Return True while this worker's run of ``stage`` is still wanted.

    ``check_pipeline_seq`` answers only "has the content changed?". Cancelling
    a document (``cancel_doc_jobs``) and the queue clear (``/system/clear-queue``)
    change neither the content nor the seq: they set the job row to ``error``.
    A loop that checked only the seq kept calling the embedder for a cancelled
    document until the stage timed out, while every counter said idle (#620).
    Polling the job row catches both, and any future stop that writes ``error``,
    without giving cancellation a second meaning of "new generation".

    The checks are ``mark_stage_running``'s, re-asked mid-run: the document
    exists at ``worker_seq`` and its job row for ``stage`` exists at that
    generation and has not been set to ``error``. Column reads, not ORM
    objects, so the session's identity map cannot hand back a stale status.
    Under execute_job it also checks the row is still this worker's claim
    (``claimed_run``): a cancel followed by a same-generation re-enqueue puts
    the row back to ``queued``, which no longer reads as cancelled, and the
    old run would otherwise carry on and complete the new one.
    """
    row = session.execute(
        select(Document.pipeline_seq, IngestionJob.pipeline_seq, IngestionJob.status, IngestionJob.started_at)
        .select_from(Document)
        .outerjoin(
            IngestionJob,
            (IngestionJob.doc_id == Document.doc_id) & (IngestionJob.stage == stage),
        )
        .where(Document.doc_id == doc_id)
    ).first()
    if row is None:
        return False
    doc_seq, job_seq, job_status, job_started_at = row
    if (doc_seq or 0) != worker_seq:
        return False
    if job_status is None or job_seq != worker_seq:
        return False
    if job_status == JobStatus.error:
        return False
    return not claim_superseded(doc_id, stage, job_started_at)


# The claim the main thread's running stage made (worker/entry.py execute_job):
# (doc_id, stage, the job row's started_at as claimed). claim_next_job stamps
# started_at on every claim and enqueue_stage clears it on every re-enqueue, so
# the timestamp identifies one claim of a row. Thread-local: a stage runs on
# the thread that claimed it; a stage run outside execute_job has no claim and
# is not checked.
_claims = threading.local()


@contextmanager
def claimed_run(doc_id: uuid.UUID, stage: JobStage, started_at):
    """Mark the enclosed stage run as the claim whose row had ``started_at``."""
    _claims.current = (doc_id, stage, started_at)
    try:
        yield
    finally:
        _claims.current = None


def claim_superseded(doc_id: uuid.UUID, stage: JobStage, job_started_at) -> bool:
    """Whether the job row now belongs to a different claim than the run in progress."""
    claim = getattr(_claims, "current", None)
    if claim is None or claim[0] != doc_id or claim[1] != stage:
        return False
    return job_started_at != claim[2]


# stage → (queue_name, timeout_seconds, pipeline_status_while_running)
STAGE_CONFIG: dict[JobStage, tuple[str, int, PipelineStatus]] = {
    JobStage.extract: ("io", 600, PipelineStatus.extracting),
    JobStage.ocr: ("cpu", 7200, PipelineStatus.ocr_running),
    JobStage.chunk: ("io", 1200, PipelineStatus.chunking),
    JobStage.entities: ("io", 900, PipelineStatus.extracting_entities),
    # Granite-R2 is ~5× slower per chunk than e5-small; bumped from 1800s for embedding-v2.
    JobStage.embed: ("cpu", 3600, PipelineStatus.embedding),
    JobStage.summarize: ("llm", 900, PipelineStatus.summarizing),
    JobStage.finalize: ("io", 600, PipelineStatus.finalizing),
}

# Retry-later for the embed stage (#619). The embedder client's own retries
# (embedder_client.py, ~12-24 s) are sized to outlast a restart; the memory
# pressure that failed 32 documents in #619 lasted minutes. When those retries
# run out on a service failure (EmbedderUnavailableError), the job goes back to
# `queued` with a `retry_after` instead of `error`, and claim_next_job skips it
# until then. The delays sum to ~63 minutes, past every outage measured there;
# a document still failing after the last one is broken, or the embedder is,
# and it ends `error` with the reason. Attempts count consecutive deferrals
# without progress: an attempt that embedded a batch starts the count over,
# since the chunks it stored are kept and a slow, progressing document is not
# a broken one.
EMBED_RETRY_REASON = "embedder_unavailable"
EMBED_RETRY_DELAYS_SECONDS = (60, 120, 300, 600, 900, 1800)
EMBED_RETRY_MAX_ATTEMPTS = len(EMBED_RETRY_DELAYS_SECONDS) + 1


def without_embed_retry_schedule(metrics: dict | None, stage: JobStage) -> dict:
    """``metrics`` minus a deferred embed's schedule, for a job requeued for another reason.

    A job requeued by the shutdown deadline or the reaper kept ``reason`` and a
    past ``retry_after``, and status-summary then reported "the embedding
    service is failing … next retry <a past time>". ``retry_attempts`` stays:
    an interruption is not progress, and the count still bounds a broken
    document. Embed rows only: a summarize row's ``retry_after`` is its Apple
    Intelligence backoff (worker/entry.py AFM_RETRY_*), which a requeue must keep.
    """
    cleaned = dict(metrics or {})
    if stage != JobStage.embed:
        return cleaned
    if cleaned.get("reason") == EMBED_RETRY_REASON:
        cleaned.pop("reason")
    cleaned.pop("retry_after", None)
    return cleaned


EMBED_RETRY_STATE_KEYS = (
    "reason",
    "retry_attempts",
    "retry_after",
    "last_error",
    "last_failed_at",
    "retry_exhausted",
)


def without_embed_retry_state(metrics: dict | None) -> dict:
    """``metrics`` minus everything retry-later recorded on an embed row (for a finished embed)."""
    cleaned = dict(metrics or {})
    if cleaned.get("reason") not in (None, EMBED_RETRY_REASON):
        return cleaned  # someone else's reason; leave the row alone
    return {k: v for k, v in cleaned.items() if k not in EMBED_RETRY_STATE_KEYS}


# Whether the main thread is inside a section the worker's SIGALRM must not
# interrupt (worker/entry.py: the stage timeout and the shutdown deadline).
# Thread-local because signal handlers run on the main thread, and only the
# main thread's sections matter to them.
_alarm_sections = threading.local()


@contextmanager
def alarm_deferred():
    """Hold the worker's alarm off for the enclosed block (it re-checks a second later)."""
    _alarm_sections.depth = getattr(_alarm_sections, "depth", 0) + 1
    try:
        yield
    finally:
        _alarm_sections.depth -= 1


def alarm_is_deferred() -> bool:
    return getattr(_alarm_sections, "depth", 0) > 0


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

# Parallel stages: fan out after chunk, fan in before finalize.
# Summarize used to live here, but it's NOT a retrieval prerequisite — chunks'
# FTS columns are populated by Postgres on insert, embeddings come from embed,
# entity search comes from entities. Summary is decoration consumed only by
# kb_get_document / kb_list_recent / kb_corpus_overview / kb_find_related and
# the frontend display. Keeping summarize in the fan-in gated docs from
# becoming retrievable on `pipeline_status = ready` until the (often slow)
# LLM call returned. Now summarize is enqueued in parallel with the fan-in
# trio but does NOT gate finalize.
_PARALLEL_STAGES = frozenset({JobStage.entities, JobStage.embed})

# Background side-channel: enqueued at the same time as the fan-in trio but
# never blocks finalize. Doc reaches `ready` once {entities, embed} land plus
# finalize; summarize runs whenever the dedicated `llm` queue worker is free
# and yields to interactive chat/research workloads (see worker/stages/
# summarize.py:_user_llm_active).
_BACKGROUND_STAGES = frozenset({JobStage.summarize})

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


async def _notify_queue_async(session: AsyncSession, queue_name: str) -> None:
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": f"job_enqueued_{queue_name}", "payload": "bulk"},
    )


async def queue_extract_for_current_pipeline(
    session: AsyncSession,
    doc_id: uuid.UUID,
    *,
    priority: int = 0,
) -> int | None:
    """Queue extract for the document's current generation in one transaction.

    Used by create/confirm paths where the caller has already created or
    updated the Document row in the same async session.
    """
    result = await session.execute(
        text(
            """
            WITH target_doc AS (
                UPDATE documents
                SET pipeline_status = 'extracting'::pipeline_status,
                    error = NULL
                WHERE doc_id = :doc_id
                  AND status = 'active'
                RETURNING doc_id, pipeline_seq
            ),
            queued_extract AS (
                INSERT INTO ingestion_jobs (
                    doc_id,
                    stage,
                    status,
                    pipeline_seq,
                    progress_current,
                    progress_total,
                    metrics,
                    priority,
                    error,
                    created_at,
                    started_at,
                    finished_at,
                    heartbeat_at
                )
                SELECT
                    doc_id,
                    'extract'::job_stage,
                    'queued'::job_status,
                    pipeline_seq,
                    0,
                    0,
                    '{}'::jsonb,
                    :priority,
                    NULL,
                    now(),
                    NULL,
                    NULL,
                    NULL
                FROM target_doc
                ON CONFLICT (doc_id, stage) DO UPDATE SET
                    status = 'queued'::job_status,
                    pipeline_seq = EXCLUDED.pipeline_seq,
                    progress_current = 0,
                    progress_total = 0,
                    metrics = '{}'::jsonb,
                    priority = EXCLUDED.priority,
                    error = NULL,
                    created_at = now(),
                    started_at = NULL,
                    finished_at = NULL,
                    heartbeat_at = NULL
                RETURNING pipeline_seq
            )
            SELECT pipeline_seq FROM queued_extract
            """
        ),
        {"doc_id": doc_id, "priority": priority},
    )
    row = result.first()
    if row is None:
        return None
    await _notify_queue_async(session, "io")
    return int(row[0])


async def reset_and_queue_extract_for_doc(
    session: AsyncSession,
    doc_id: uuid.UUID,
    *,
    priority: int = 0,
) -> int | None:
    """Bump a document generation, reset jobs, and queue extract atomically."""
    result = await session.execute(
        text(
            """
            WITH target_doc AS (
                UPDATE documents
                SET pipeline_status = 'extracting'::pipeline_status,
                    pipeline_seq = COALESCE(pipeline_seq, 0) + 1,
                    error = NULL
                WHERE doc_id = :doc_id
                  AND status = 'active'
                RETURNING doc_id, pipeline_seq
            ),
            deleted_other_jobs AS (
                DELETE FROM ingestion_jobs AS job
                USING target_doc
                WHERE job.doc_id = target_doc.doc_id
                  AND job.stage <> 'extract'::job_stage
                RETURNING job.job_id
            ),
            queued_extract AS (
                INSERT INTO ingestion_jobs (
                    doc_id,
                    stage,
                    status,
                    pipeline_seq,
                    progress_current,
                    progress_total,
                    metrics,
                    priority,
                    error,
                    created_at,
                    started_at,
                    finished_at,
                    heartbeat_at
                )
                SELECT
                    doc_id,
                    'extract'::job_stage,
                    'queued'::job_status,
                    pipeline_seq,
                    0,
                    0,
                    '{}'::jsonb,
                    :priority,
                    NULL,
                    now(),
                    NULL,
                    NULL,
                    NULL
                FROM target_doc
                ON CONFLICT (doc_id, stage) DO UPDATE SET
                    status = 'queued'::job_status,
                    pipeline_seq = EXCLUDED.pipeline_seq,
                    progress_current = 0,
                    progress_total = 0,
                    metrics = '{}'::jsonb,
                    priority = EXCLUDED.priority,
                    error = NULL,
                    created_at = now(),
                    started_at = NULL,
                    finished_at = NULL,
                    heartbeat_at = NULL
                RETURNING pipeline_seq
            )
            SELECT pipeline_seq FROM queued_extract
            """
        ),
        {"doc_id": doc_id, "priority": priority},
    )
    row = result.first()
    if row is None:
        return None
    await _notify_queue_async(session, "io")
    return int(row[0])


def enqueue_stage(doc_id: uuid.UUID, stage: JobStage, *, priority: int = 0) -> None:
    """Upsert IngestionJob row as queued and notify workers."""
    queue_name, timeout, running_status = STAGE_CONFIG[stage]

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        current_seq = doc.pipeline_seq or 0

        # Upsert ingestion job
        existing = session.execute(
            select(IngestionJob).where(
                IngestionJob.doc_id == doc_id,
                IngestionJob.stage == stage,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status in (JobStatus.queued, JobStatus.running) and existing.pipeline_seq == current_seq:
                # Already enqueued or running (e.g. concurrent advance_pipeline calls) — skip
                session.close()
                return
            existing.status = JobStatus.queued
            existing.pipeline_seq = current_seq
            existing.error = None
            existing.progress_current = 0
            existing.progress_total = 0
            existing.started_at = None
            existing.finished_at = None
            existing.heartbeat_at = None
            # Re-enqueue is logically a fresh cycle; the depth-history query
            # in /system/summary-backlog uses created_at <= ts AND
            # (finished_at IS NULL OR finished_at > ts) to estimate
            # in-flight-at-time-T. Without this reset, a re-enqueued job
            # (e.g. via /resummarize) would be counted as in-flight for
            # the entire span back to its original enqueue, including the
            # period when it was actually `done`.
            existing.created_at = datetime.now(UTC)
            existing.priority = priority
            if stage == JobStage.embed:
                # The embed row's metrics hold only its retry-later state
                # (worker/entry.py). A fresh cycle starts that budget afresh and
                # must not inherit a retry_after that would hold the claim back.
                existing.metrics = {}
        else:
            job = IngestionJob(
                doc_id=doc_id,
                stage=stage,
                status=JobStatus.queued,
                pipeline_seq=current_seq,
                priority=priority,
            )
            session.add(job)

        # Update doc pipeline_status — background stages (summarize) don't
        # touch it. The doc may already be `ready` when summarize is
        # enqueued, and we don't want the badge to regress. See the
        # matching guard in mark_stage_done.
        if stage not in _BACKGROUND_STAGES:
            doc.pipeline_status = running_status
        doc.error = None

        session.commit()

        filename = _doc_filename(doc, session)

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


# Orphaned running jobs (the worker died). One definition for the status
# summary's stuck count, the admin "Recover stuck jobs" route
# (/system/reaper-run) and the background reaper (api/app.py
# _session_reaper_loop), so what is shown as stuck is what gets recovered.
# Workers heartbeat every 30 s (worker/entry.py HEARTBEAT_INTERVAL).
ORPHAN_HEARTBEAT_SECONDS = 90


def is_orphaned_running_job(now: datetime, job: IngestionJob) -> bool:
    """Whether a ``running`` row's worker is gone: its heartbeat is at least
    ``ORPHAN_HEARTBEAT_SECONDS`` old or, with no heartbeat, the row has been
    running (since ``started_at``, else ``created_at``) for twice the stage timeout."""
    if job.heartbeat_at is not None:
        return (now - job.heartbeat_at).total_seconds() >= ORPHAN_HEARTBEAT_SECONDS
    _, timeout, _ = STAGE_CONFIG.get(job.stage, ("io", 600, PipelineStatus.error))
    since = job.started_at or job.created_at
    return since is not None and (now - since).total_seconds() >= timeout * 2


async def requeue_orphan_job(session: AsyncSession, job: IngestionJob) -> bool:
    """Put one orphan back in the queue, in place, if it is still the row that was read.

    Not ``enqueue_stage``: it skips a row already ``running`` at the current
    pipeline_seq, which an orphan always is, so the route used to report jobs
    reaped while every one stayed ``running`` (#864). The UPDATE is conditional
    on the status and claim (``started_at``, ``heartbeat_at``) as read: if
    another reaper requeued the row and a worker claimed it since, or its
    worker heartbeated, it is a live run and is left alone. Returns whether the
    row was requeued. Keeps the generation; the caller commits.
    """
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.job_id == job.job_id,
            IngestionJob.status == JobStatus.running,
            IngestionJob.started_at.is_not_distinct_from(job.started_at),
            IngestionJob.heartbeat_at.is_not_distinct_from(job.heartbeat_at),
        )
        .values(
            status=JobStatus.queued,
            started_at=None,
            heartbeat_at=None,
            error=None,
            progress_current=0,
            progress_total=0,
            # An orphan is not an embed deferred for an outage (#619):
            # drop any stale retry schedule so status stays truthful.
            metrics=without_embed_retry_schedule(job.metrics, job.stage),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def requeue_orphaned_jobs(session: AsyncSession, now: datetime) -> list[tuple[uuid.UUID, JobStage]]:
    """Requeue every orphaned ``running`` job of an active document at its current generation.

    Both reapers call this. Returns the (doc_id, stage) of the rows actually
    requeued; the caller commits, then passes them to ``announce_requeued_jobs``.
    """
    running = (
        (
            await session.execute(
                select(IngestionJob)
                .join(Document, Document.doc_id == IngestionJob.doc_id)
                .where(
                    Document.status == "active",
                    IngestionJob.status == JobStatus.running,
                    IngestionJob.pipeline_seq == Document.pipeline_seq,
                )
            )
        )
        .scalars()
        .all()
    )
    requeued: list[tuple[uuid.UUID, JobStage]] = []
    for job in running:
        if not is_orphaned_running_job(now, job):
            continue
        doc_id, stage, heartbeat_at = job.doc_id, job.stage, job.heartbeat_at
        if not await requeue_orphan_job(session, job):
            logger.info("Reaper: orphan doc=%s stage=%s changed since it was read; left as it is", doc_id, stage.value)
            continue
        logger.warning("Reaper: re-queued orphan job doc=%s stage=%s (heartbeat=%s)", doc_id, stage.value, heartbeat_at)
        requeued.append((doc_id, stage))
    return requeued


def announce_requeued_jobs(requeued: list[tuple[uuid.UUID, JobStage]]) -> None:
    """After the requeue commits, announce each job the way ``enqueue_stage`` does.

    A pg_notify on the stage's queue channel, so a waiting worker claims it now
    rather than at its idle poll, and a "queued" job event, so the live view
    stops showing it running. Sync (its own session): call it from async code
    through ``asyncio.to_thread``.
    """
    if not requeued:
        return
    session = get_sync_session()
    try:
        filenames: dict[uuid.UUID, str] = {}
        for doc_id, stage in requeued:
            doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
            filenames[doc_id] = _doc_filename(doc, session) if doc is not None else ""
            session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": f"job_enqueued_{STAGE_CONFIG[stage][0]}", "payload": str(doc_id)},
            )
        session.commit()
    finally:
        session.close()
    for doc_id, stage in requeued:
        publish_job_event(doc_id, stage.value, "queued", filename=filenames[doc_id])


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

    doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
    current_seq = doc.pipeline_seq or 0

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
                pipeline_seq=current_seq,
                started_at=now,
                finished_at=now,
                metrics=metrics,
                priority=priority,
            )
        )
    else:
        existing.status = JobStatus.done
        existing.pipeline_seq = current_seq
        existing.finished_at = now
        existing.metrics = metrics
        existing.priority = priority

    doc.pipeline_status = pipeline_status
    session.commit()
    publish_job_event(doc_id, stage.value, "done", filename=filename)


def advance_pipeline(doc_id: uuid.UUID) -> None:
    """Determine the next stage(s) and enqueue them.

    Three phases:
      1. Sequential prefix: extract → [ocr] → chunk (linear, same as before)
      2. Fan-out: entities + embed enqueued after chunk; summarize enqueued
         alongside as a background stage (does NOT gate finalize)
      3. Fan-in: finalize (only when entities + embed are done)
    """
    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        filename = _doc_filename(doc, session)
        current_seq = doc.pipeline_seq or 0

        # Gather completed and in-flight stages
        all_jobs = (
            session.execute(
                select(IngestionJob).where(
                    IngestionJob.doc_id == doc_id,
                    IngestionJob.pipeline_seq == current_seq,
                )
            )
            .scalars()
            .all()
        )
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

        # --- Phase 2: Fan-out (entities + embed) + background (summarize) ---
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

        # Background stages: enqueued the first time we see chunk done, then
        # left alone. The check is "any existing job row for this stage" —
        # not just queued/running/done — otherwise an errored summarize gets
        # re-enqueued on every advance_pipeline tick. That has two bad
        # effects: (1) it loops indefinitely, retrying the same broken
        # summary, and (2) when the re-enqueue happens we'd return below
        # before reaching the phase-3 finalize gate, so finalize would
        # never fire for any doc whose summarize errored. Admin endpoints
        # (/system/resummarize-all, /docs/X/resummarize) still re-enqueue
        # explicitly via enqueue_stage(), which handles its own upsert.
        existing_stages = {j.stage for j in all_jobs}
        background_to_enqueue: list[JobStage] = []
        if JobStage.chunk in completed:
            for stage in _BACKGROUND_STAGES:
                if stage in existing_stages:
                    continue
                background_to_enqueue.append(stage)

        all_new = to_enqueue + background_to_enqueue
        if all_new:
            session.commit()
            session.close()
            for stage in all_new:
                enqueue_stage(doc_id, stage, priority=priority)
            return

        # --- Phase 3: Fan-in (finalize) — gates only on _PARALLEL_STAGES ---
        if completed >= _PARALLEL_STAGES and JobStage.finalize not in completed and JobStage.finalize not in in_flight:
            session.commit()
            session.close()
            enqueue_stage(doc_id, JobStage.finalize, priority=priority)
            return

        # Only restore to ready if the gating stages are actually done (not
        # just nothing to enqueue). Background stages (summarize) are NOT
        # in the gate — a still-pending summary is normal post-finalize.
        all_stages_done = (
            JobStage.finalize in completed
            and not (in_flight - _BACKGROUND_STAGES)
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
    """Mark a stage as done and advance the pipeline, atomically with respect to the worker's alarm.

    Committing `done` and advancing are two steps. A shutdown deadline (or stage
    timeout) firing between them left the job `done` with nothing enqueued after
    it, and the deadline's requeue matches only `running` rows, so the document
    stranded. The alarm is held off until both have run.
    """
    with alarm_deferred():
        _mark_stage_done(doc_id, stage, worker_seq=worker_seq, **extra_event_fields)


def _mark_stage_done(
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
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc is None:
            logger.warning(
                "mark_stage_done: doc %s no longer exists, skipping mark-done for %s",
                doc_id,
                stage.value,
            )
            return
        current_seq = doc.pipeline_seq or 0
        if worker_seq is None:
            worker_seq = current_seq

        # Race check: did pipeline_seq bump while we were running?
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
        if job.pipeline_seq != worker_seq:
            logger.info(
                "mark_stage_done: job generation mismatch for doc=%s stage=%s (worker=%s, job=%s)",
                doc_id,
                stage.value,
                worker_seq,
                job.pipeline_seq,
            )
            return
        if job.status == JobStatus.error:
            # Cancelled (or queue-cleared) while running. A stage that stops on
            # a cancel still calls this on its way out, per the every-exit-path
            # rule; marking the row done here would overwrite "Cancelled by
            # user" and advance the pipeline the user just stopped (#620).
            logger.info(
                "mark_stage_done: %s for doc %s was cancelled while running (%s), leaving it",
                stage.value,
                doc_id,
                job.error,
            )
            return
        if claim_superseded(doc_id, stage, job.started_at):
            # Re-enqueued (or re-claimed) since this run claimed it: the row is
            # a newer run's, which must not be completed by this one.
            logger.info(
                "mark_stage_done: %s for doc %s was re-enqueued while this run ran, leaving it to the new claim",
                stage.value,
                doc_id,
            )
            return
        job.status = JobStatus.done
        job.finished_at = datetime.now(UTC)
        if stage == JobStage.summarize and (job.metrics or {}).get("reason") == "apple_intelligence_unavailable":
            job.metrics = {}
        if stage == JobStage.embed and job.metrics:
            # A done row must not carry the failure state of the deferrals
            # that preceded it (worker/entry.py _record_embed_unavailable_job).
            job.metrics = without_embed_retry_state(job.metrics)

        # Background stages (summarize) never touch pipeline_status — that
        # field reflects the gating-stage progression only. A summarize-
        # before-finalize finish would otherwise prematurely set
        # pipeline_status = summarized; a summarize-after-finalize finish
        # would regress it from ready back to summarized. Either is wrong.
        if stage not in _BACKGROUND_STAGES:
            doc.pipeline_status = done_status
        filename = _doc_filename(doc, session)

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
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one_or_none()
        if doc is None:
            logger.info(
                "Skipping %s for doc %s — doc no longer exists",
                stage.value,
                doc_id,
            )
            return False
        current_seq = doc.pipeline_seq or 0
        if worker_seq is None:
            worker_seq = current_seq
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
        if job.pipeline_seq != worker_seq:
            logger.info(
                "Skipping %s for doc %s — job generation mismatch (worker=%s, job=%s)",
                stage.value,
                doc_id,
                worker_seq,
                job.pipeline_seq,
            )
            return False
        if job.status == JobStatus.error:
            logger.info(
                "Skipping %s for doc %s — already cancelled/errored",
                stage.value,
                doc_id,
            )
            return False
        filename = _doc_filename(doc, session)
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
            filename = _doc_filename(doc, session)
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
