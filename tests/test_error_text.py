"""Tests for describe_error and its call sites.

The health endpoint is the surface that got noticed, but the ones that matter
most are the worker's: `worker/entry.py` writes its rendering into
`ingestion_jobs.error` and `documents.error`, which is what a user reads as the
reason a document failed. Those had no coverage at all — reverting either line
to `f"{type(e).__name__}: {e}"` passed the entire suite.
"""

import httpx
import pytest

from harbor_clerk.error_text import HEALTH_DETAIL_LIMIT, describe_error

# --------------------------------------------------------------------------
# the helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        TimeoutError(),
        ValueError(""),
        RuntimeError("   "),
    ],
)
def test_empty_message_still_names_the_type(exc):
    """These are exactly the exceptions worth naming, and exactly the ones that
    stringify to nothing."""
    rendered = describe_error(exc)
    assert rendered == type(exc).__name__
    assert not rendered.endswith(":")
    assert rendered.strip()


def test_keeps_the_message_when_there_is_one():
    assert describe_error(httpx.ConnectError("connection refused")) == "ConnectError: connection refused"


def test_strips_surrounding_whitespace():
    assert describe_error(ValueError("  spaced  ")) == "ValueError: spaced"


def test_max_detail_boundary():
    """Truncate above the limit, leave alone at exactly the limit."""
    at_limit = describe_error(ValueError("x" * HEALTH_DETAIL_LIMIT), max_detail=HEALTH_DETAIL_LIMIT)
    assert at_limit.endswith("x")
    assert "…" not in at_limit

    over_limit = describe_error(ValueError("x" * (HEALTH_DETAIL_LIMIT + 1)), max_detail=HEALTH_DETAIL_LIMIT)
    assert over_limit.endswith("…")
    assert len(over_limit) < HEALTH_DETAIL_LIMIT + 40


def test_max_detail_never_truncates_the_type_name():
    rendered = describe_error(httpx.ConnectError("y" * 5000), max_detail=10)
    assert rendered.startswith("ConnectError: ")


def test_unbounded_by_default():
    assert len(describe_error(ValueError("z" * 1000))) > 1000


# --------------------------------------------------------------------------
# the call sites that reach users
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_records_a_named_error_on_the_document(db_session, monkeypatch):
    """A stage failing with an empty-message exception must still tell the user
    what kind of failure it was.

    This is the #555 surface: `documents.error` is rendered verbatim in the
    failed-documents list. Before this, a timing-out embedder produced the
    literal string "ReadTimeout: " — a dangling colon and nothing else.
    """
    import hashlib

    from sqlalchemy import select

    from harbor_clerk.db_sync import get_sync_session
    from harbor_clerk.models import Document, IngestionJob
    from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
    from harbor_clerk.worker import entry as worker_entry

    doc = Document(
        title="err-fixture",
        canonical_filename="err-fixture.txt",
        status="active",
        sha256=hashlib.sha256(b"err-fixture").digest(),
        pipeline_status=PipelineStatus.chunked,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.embed, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    def _boom(_doc_id, *, worker_seq=None):
        raise httpx.ReadTimeout("")

    monkeypatch.setitem(worker_entry.STAGE_FUNCTIONS, JobStage.embed, _boom)
    worker_entry.execute_job(doc_id, JobStage.embed, pipeline_seq=0)

    session = get_sync_session()
    try:
        refreshed = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        job = session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.embed)
        ).scalar_one()
    finally:
        session.close()

    for label, value in (("documents.error", refreshed.error), ("ingestion_jobs.error", job.error)):
        assert value, f"{label} is empty — the user is shown no reason at all"
        assert "ReadTimeout" in value, f"{label} does not name the failure: {value!r}"
        assert not value.rstrip().endswith(":"), f"{label} ends in a dangling colon: {value!r}"
