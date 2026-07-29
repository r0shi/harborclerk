"""`POST /api/docs/reprocess-failed` — bulk recovery for a failed batch (#554).

Recovering from a transient outage previously meant clicking through the
Documents list 100 at a time, because selection is per-page and the only bulk
action was `/system/reprocess-all`, which re-runs the whole corpus including
everything that succeeded.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header

pytestmark = pytest.mark.anyio


def _doc(title: str, *, sha: bytes, status: str = "active", pipeline_status=PipelineStatus.error) -> Document:
    return Document(
        title=title,
        status=status,
        sha256=sha,
        pipeline_status=pipeline_status,
        pipeline_seq=3,
        error="embedder unreachable",
    )


async def test_requeues_only_the_failed_active_documents(client, admin_user, admin_token, db_session):
    """The whole point: the ones that worked must not be touched.

    `/system/reprocess-all` already existed and re-runs everything — if this
    endpoint did the same it would be a slower spelling of that, and it would
    throw away good extractions to recover a handful of bad ones.
    """
    failed = _doc("Failed", sha=b"f" * 32)
    ready = _doc("Ready", sha=b"r" * 32, pipeline_status=PipelineStatus.ready)
    # "deleted" is what the live corpus actually uses for non-active rows.
    inactive = _doc("Deleted failed", sha=b"i" * 32, status="deleted")
    db_session.add_all([failed, ready, inactive])
    await db_session.flush()
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 1, "matched": 1, "remaining": 0}

    for doc, expected_seq, expected_status in (
        (failed, 4, PipelineStatus.extracting),
        (ready, 3, PipelineStatus.ready),
        (inactive, 3, PipelineStatus.error),
    ):
        await db_session.refresh(doc)
        assert doc.pipeline_seq == expected_seq, f"{doc.title} pipeline_seq"
        assert doc.pipeline_status == expected_status, f"{doc.title} pipeline_status"

    await db_session.refresh(failed)
    assert failed.error is None, "the stale error must be cleared, or the UI still shows it as failed"


async def test_queues_an_extract_job_for_the_new_generation(client, admin_user, admin_token, db_session):
    """A requeued document that never runs is the failure this must not have.

    The row is picked up by pipeline_seq, so an extract job carrying the *old*
    seq is invisible to the worker — the document would sit in `extracting`
    forever, looking busy.
    """
    failed = _doc("Failed", sha=b"f" * 32)
    db_session.add(failed)
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=failed.doc_id,
            stage=JobStage.chunk,
            status=JobStatus.error,
            pipeline_seq=3,
        )
    )
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text

    jobs = (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == failed.doc_id))).scalars().all()
    stages = {(j.stage, j.status, j.pipeline_seq) for j in jobs}
    assert (JobStage.extract, JobStatus.queued, 4) in stages, f"no queued extract at the new seq: {stages}"
    assert not [j for j in jobs if j.stage == JobStage.chunk], "the stale chunk job should have been cleared"


async def test_limit_reports_what_it_left_behind(client, admin_user, admin_token, db_session):
    """A silent cap reads as 'everything is recovered' when it isn't."""
    db_session.add_all([_doc(f"Failed {i}", sha=bytes([i]) * 32) for i in range(5)])
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed?limit=2", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 2, "matched": 5, "remaining": 3}


async def test_no_failed_documents_is_not_an_error(client, admin_user, admin_token, db_session):
    db_session.add(_doc("Ready", sha=b"r" * 32, pipeline_status=PipelineStatus.ready))
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 0, "matched": 0, "remaining": 0}


async def test_the_literal_path_is_not_swallowed_by_the_doc_id_route(client, admin_user, admin_token, db_session):
    """`/docs/{doc_id}/reprocess` sits beside this; a UUID parse of the literal
    segment is the classic way a route like this 422s in production and passes
    in a unit test that calls the function directly."""
    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, f"literal route matched the wrong handler: {resp.status_code} {resp.text}"


async def test_requires_admin(client, regular_user, user_token, db_session):
    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(user_token))
    assert resp.status_code in (401, 403), f"a non-admin requeued the corpus: {resp.status_code}"


async def test_a_row_that_stopped_matching_is_not_counted(client, admin_user, admin_token, db_session, monkeypatch):
    """The count has to mean something, or "requeued: 126" is just len(selected).

    `reset_and_queue_extract_for_doc` returns None when the row no longer
    matches by the time the UPDATE lands — deleted, deactivated, or already
    requeued by the watcher between the SELECT and the write. Counting the
    attempt instead of the result reports work that did not happen, and
    `remaining` is derived from it, so the caller is told to stop retrying.
    """
    import harbor_clerk.worker.pipeline as pipeline

    db_session.add_all([_doc("A", sha=b"a" * 32), _doc("B", sha=b"b" * 32)])
    await db_session.commit()

    real = pipeline.reset_and_queue_extract_for_doc
    seen: list[object] = []

    async def _one_vanishes(session, doc_id, **kw):
        seen.append(doc_id)
        if len(seen) == 1:
            return None  # as if the row had just been deleted
        return await real(session, doc_id, **kw)

    monkeypatch.setattr(pipeline, "reset_and_queue_extract_for_doc", _one_vanishes)

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 1, "matched": 2, "remaining": 1}, (
        "a row that stopped matching was counted as requeued"
    )
