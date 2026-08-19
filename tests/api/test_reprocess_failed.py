"""`POST /api/docs/reprocess-failed` — bulk recovery for a failed batch (#554).

Recovering from a transient outage previously meant clicking through the
Documents list 100 at a time, because selection is per-page and the only bulk
action was `/system/reprocess-all`, which re-runs the whole corpus including
everything that succeeded.
"""

from __future__ import annotations

from sqlalchemy import select, update

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header

# No pytest.mark.anyio: pyproject sets asyncio_mode = "auto", so pytest-asyncio
# already runs these. Adding the anyio marker routes them through a second
# plugin and a second event loop, while the `_engine` fixture is session-scoped
# and bound to the first — "attached to a different loop". It passed locally
# and failed every test in CI.


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
    assert resp.json() == {"requeued": 1, "remaining": 0}

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
    assert resp.json() == {"requeued": 2, "remaining": 3}


async def test_no_failed_documents_is_not_an_error(client, admin_user, admin_token, db_session):
    db_session.add(_doc("Ready", sha=b"r" * 32, pipeline_status=PipelineStatus.ready))
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 0, "remaining": 0}


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
            # Actually stop the row matching, rather than only returning None.
            # A mock that returns None while leaving the row in `error` is
            # testing its own stub: the row really does still match, so
            # remaining=1 is the right answer and the assertion proves nothing
            # about the counting.
            await session.execute(update(Document).where(Document.doc_id == doc_id).values(status="deleted"))
            return None
        return await real(session, doc_id, **kw)

    monkeypatch.setattr(pipeline, "reset_and_queue_extract_for_doc", _one_vanishes)

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    # remaining is 0: the vanished row no longer matches, and the other was
    # requeued. Counting before the loop reported 1 here — telling the caller to
    # keep retrying work that no longer existed.
    assert resp.json() == {"requeued": 1, "remaining": 0}, (
        "a row that stopped matching was counted as requeued, or inflated remaining"
    )


async def test_deliberately_stopped_documents_are_left_alone(client, admin_user, admin_token, db_session):
    """Cancelling and /system/clear-queue both park a document in `error`.

    So a naive "requeue everything in error" restarts work an admin explicitly
    stopped — and silently undoes the emergency stop. The distinguishing signal
    is only the error text.
    """
    cancelled = _doc("Cancelled", sha=b"c" * 32)
    cancelled.error = "Cancelled by user"
    cleared = _doc("Cleared", sha=b"q" * 32)
    cleared.error = "Queue cleared before pipeline completed"
    genuine = _doc("Genuine", sha=b"g" * 32)
    db_session.add_all([cancelled, cleared, genuine])
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"requeued": 1, "remaining": 0}, "a deliberately-stopped document was restarted"

    for doc in (cancelled, cleared):
        await db_session.refresh(doc)
        assert doc.pipeline_status == PipelineStatus.error, f"{doc.title} was restarted"
        assert doc.pipeline_seq == 3, f"{doc.title} generation was bumped"


def test_the_stop_sentinels_still_match_their_call_sites():
    """The exclusion is by string match, so a reword elsewhere fails open —
    silently restarting cancelled work with every test still green."""
    from pathlib import Path

    from harbor_clerk.api.routes.documents import DELIBERATELY_STOPPED_ERRORS

    src = Path(__file__).resolve().parents[2] / "src" / "harbor_clerk"
    corpus = (src / "worker" / "pipeline.py").read_text() + (src / "api" / "routes" / "system.py").read_text()
    missing = [text for text in DELIBERATELY_STOPPED_ERRORS if f'"{text}"' not in corpus]
    assert not missing, (
        f"these sentinels no longer appear in pipeline.py or system.py: {missing}. "
        "The writer was reworded, so reprocess-failed will restart that population again."
    )


async def test_the_bulk_requeue_is_audited(client, admin_user, admin_token, db_session):
    """An admin action that mutates the corpus in bulk has to leave a record."""
    from harbor_clerk.models import AuditLog

    db_session.add(_doc("Failed", sha=b"f" * 32))
    await db_session.commit()

    resp = await client.post("/api/docs/reprocess-failed", headers=auth_header(admin_token))
    assert resp.status_code == 202, resp.text

    entries = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "reprocess_failed_documents")))
        .scalars()
        .all()
    )
    assert len(entries) == 1, f"expected one audit entry, got {len(entries)}"
    assert entries[0].detail == {"requeued": 1}, entries[0].detail
