"""Summarize stage — generate adaptive document summary from all chunks."""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from harbor_clerk.config import refresh_llm_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.health import document_wait_budget
from harbor_clerk.llm.summarize import (
    AppleIntelligenceDocumentError,
    AppleIntelligenceUnavailableError,
    classify_doc_type,
    generate_summary,
)
from harbor_clerk.models import ChatMessage, Chunk, Document, IngestionJob
from harbor_clerk.models.enums import JobStage
from harbor_clerk.models.research_state import ResearchState
from harbor_clerk.worker.pipeline import (
    mark_stage_done,
    mark_stage_running,
    stage_run_is_current,
)

logger = logging.getLogger(__name__)


class _SummarizeRunStopped(Exception):
    """Raised from generate_summary's yield_check when the job was cancelled; caught in run_summarize."""


# Yield-to-interactive: when a user-driven LLM workload is active, summarize
# stops before claiming the LLM slot and waits up to YIELD_BUDGET_SEC for
# the slot to free. Avoids long summary jobs starving chat/research with
# `-np 1` on llama-server. After the budget expires, summarize proceeds
# anyway so it isn't starved indefinitely.
_USER_ACTIVITY_WINDOW_SEC = 60  # last user-message age that counts as "active"
_YIELD_POLL_SEC = 15  # how often to re-check while yielding
_YIELD_BUDGET_SEC = 60  # total time we'll wait before proceeding anyway


def _user_llm_active() -> bool:
    """Return True if a chat or research is using (or just used) the local LLM.

    Two signals:
      - A chat user-message was created in the last _USER_ACTIVITY_WINDOW_SEC.
        Chat saves the user message at the start of chat_stream(), so a
        recent user message means a chat assistant turn is likely streaming.
      - A research_state row exists with status='running'. Research holds
        the LLM for minutes at a time across many calls.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=_USER_ACTIVITY_WINDOW_SEC)
    session = get_sync_session()
    try:
        recent_chat = session.execute(
            select(ChatMessage.message_id)
            .where(ChatMessage.role == "user")
            .where(ChatMessage.created_at > cutoff)
            .limit(1)
        ).first()
        if recent_chat:
            return True
        running_research = session.execute(
            select(ResearchState.conversation_id).where(ResearchState.status == "running").limit(1)
        ).first()
        return running_research is not None
    finally:
        session.close()


def _wait_for_idle_llm(budget_sec: int = _YIELD_BUDGET_SEC) -> None:
    """Block up to budget_sec while a user is using the LLM, polling every
    _YIELD_POLL_SEC. Returns either when the LLM is idle or the budget
    expires (so summarize is never starved indefinitely)."""
    elapsed = 0
    while elapsed < budget_sec and _user_llm_active():
        logger.info("summarize: yielding to interactive LLM workload (%ds elapsed)", elapsed)
        time.sleep(_YIELD_POLL_SEC)
        elapsed += _YIELD_POLL_SEC


def _summary_and_type(
    doc_id: uuid.UUID,
    chunks: list[str],
    mime_type: str,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    yield_check: Callable[[], None] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """The document's summary, the model that made it, and its type, computed before the race check.

    Both calls run under one wait budget (#654): under Docker Compose a load or a restart of llama-server is
    waited out at most DOCUMENT_WAIT_SECONDS for the summary and the type together, not that much each.

    ``yield_check`` (default: wait for an idle LLM, looked up at call time) runs before every map-reduce LLM
    sub-call. When it raises _SummarizeRunStopped (the run
    was cancelled or superseded), nothing more is computed: (None, None, None), with no classification call."""
    with document_wait_budget():
        try:
            summary, model_used = generate_summary(
                chunks,
                yield_check=yield_check or _wait_for_idle_llm,
                progress_callback=progress_callback,
            )
        except _SummarizeRunStopped:
            logger.info("summarize: run for %s cancelled or superseded; stopping", doc_id)
            return None, None, None
        except AppleIntelligenceUnavailableError:
            logger.warning("Summary generation blocked for %s because Apple Intelligence is unavailable", doc_id)
            raise
        except AppleIntelligenceDocumentError:
            logger.warning("Summary generation rejected for %s by Apple Intelligence", doc_id)
            raise
        except Exception:
            logger.warning("Summary generation failed for %s", doc_id, exc_info=True)
            summary, model_used = None, None

        doc_type = None
        try:
            doc_type = classify_doc_type(chunks, mime_type=mime_type)
        except Exception:
            logger.warning("Doc type classification failed for %s", doc_id, exc_info=True)
    return summary, model_used, doc_type


def run_summarize(doc_id: uuid.UUID, *, worker_seq: int | None = None) -> None:
    """Generate a summary for the document from all its chunks."""
    # Yield-to-interactive: if the user is chatting or researching right
    # now, wait up to _YIELD_BUDGET_SEC before claiming the LLM slot. This
    # makes interactive workloads feel snappy without preempting in-flight
    # summary work — between map-reduce sub-calls we also yield (see the
    # yield_check passed to generate_summary).
    _wait_for_idle_llm()

    if not mark_stage_running(doc_id, JobStage.summarize, worker_seq=worker_seq):
        return

    # Re-read LLM model from config.json in case user changed it via API
    refresh_llm_settings()

    session = get_sync_session()
    try:
        chunks = (
            session.execute(select(Chunk.chunk_text).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_num))
            .scalars()
            .all()
        )

        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        if worker_seq is None:
            worker_seq = doc.pipeline_seq

        if chunks:
            # Update the IngestionJob row's progress counters between map calls
            # so the queue tray's progress bar fills as the summary advances.
            # Per-call session: this fires many times during a long summary;
            # reusing the outer session would either commit other in-progress
            # work prematurely or risk race conditions.
            def _progress(current: int, total: int) -> None:
                # Per-call session; isolated from the outer worker session so a long
                # summary's many small UPDATEs don't fight for the same connection.
                # Errors here are intentionally swallowed: a transient DB hiccup on a
                # progress write must not invalidate the actual summary the LLM just
                # produced. Worst case the queue tray shows a stale percentage for a
                # few seconds.
                inner_session = get_sync_session()
                try:
                    inner_session.execute(
                        update(IngestionJob)
                        .where(IngestionJob.doc_id == doc_id)
                        .where(IngestionJob.stage == JobStage.summarize)
                        .where(IngestionJob.pipeline_seq == worker_seq)
                        .values(progress_current=current, progress_total=total)
                    )
                    inner_session.commit()
                except Exception:
                    logger.warning(
                        "summarize: progress update failed (current=%d, total=%d)",
                        current,
                        total,
                        exc_info=True,
                    )
                finally:
                    inner_session.close()

            def _yield_or_stop() -> None:
                # Called before every map-reduce LLM sub-call: the natural point
                # to notice a cancel or queue clear (job row set to error), or a
                # newer claim of the row, and stop spending the LLM on a document
                # nobody wants summarized by this run.
                if not stage_run_is_current(session, doc_id, JobStage.summarize, worker_seq):
                    raise _SummarizeRunStopped()
                _wait_for_idle_llm()

            # Compute before the race check, under one per-document wait budget.
            summary, model_used, doc_type = _summary_and_type(
                doc_id, list(chunks), doc.mime_type or "", progress_callback=_progress, yield_check=_yield_or_stop
            )

            # Race check before writing results: the seq, a cancel, or a newer claim.
            if not stage_run_is_current(session, doc_id, JobStage.summarize, worker_seq):
                logger.info("summarize: run for %s cancelled, re-enqueued or superseded, aborting write", doc_id)
                session.close()
                mark_stage_done(doc_id, JobStage.summarize, worker_seq=worker_seq)  # declines likewise
                return

            # Require visible content — not just truthiness — so a degraded
            # LLM that returns whitespace or zero-width characters can't
            # persist a "blank summary attributed to the current model".
            # generate_summary should already filter these via
            # _has_visible_content, but a check here is a cheap belt-and-
            # suspenders against any future regression.
            if summary and summary.strip():
                doc.summary = summary
                doc.summary_model = model_used

            if doc_type is not None:
                doc.doc_type = doc_type

            session.commit()
    finally:
        session.close()

    mark_stage_done(doc_id, JobStage.summarize, worker_seq=worker_seq)
