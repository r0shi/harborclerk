"""Summarize stage — generate adaptive document summary from all chunks."""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from harbor_clerk.config import refresh_llm_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.summarize import classify_doc_type, generate_summary
from harbor_clerk.models import ChatMessage, Chunk, Document
from harbor_clerk.models.enums import JobStage
from harbor_clerk.models.research_state import ResearchState
from harbor_clerk.worker.pipeline import check_pipeline_seq, mark_stage_done, mark_stage_running

logger = logging.getLogger(__name__)

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
            select(ChatMessage.id).where(ChatMessage.role == "user").where(ChatMessage.created_at > cutoff).limit(1)
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


def run_summarize(doc_id: uuid.UUID) -> None:
    """Generate a summary for the document from all its chunks."""
    # Yield-to-interactive: if the user is chatting or researching right
    # now, wait up to _YIELD_BUDGET_SEC before claiming the LLM slot. This
    # makes interactive workloads feel snappy without preempting in-flight
    # summary work — between map-reduce sub-calls we also yield (see the
    # yield_check passed to generate_summary).
    _wait_for_idle_llm()

    if not mark_stage_running(doc_id, JobStage.summarize):
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
        worker_seq = doc.pipeline_seq

        if chunks:
            # Generate summary (compute before race check)
            try:
                summary, model_used = generate_summary(list(chunks), yield_check=_wait_for_idle_llm)
            except Exception:
                logger.warning("Summary generation failed for %s", doc_id, exc_info=True)
                summary, model_used = None, None

            # Classify document type (compute before race check)
            doc_type = None
            try:
                doc_type = classify_doc_type(list(chunks), mime_type=doc.mime_type or "")
            except Exception:
                logger.warning("Doc type classification failed for %s", doc_id, exc_info=True)

            # Race check before writing results
            if not check_pipeline_seq(session, doc_id, worker_seq):
                logger.info("summarize: pipeline_seq bumped during processing for %s, aborting write", doc_id)
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
