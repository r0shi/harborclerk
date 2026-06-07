"""Tests for /api/system/* endpoints."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFolder
from tests.conftest import auth_header

_READY_GATE_STAGES = (JobStage.extract, JobStage.chunk, JobStage.entities, JobStage.embed, JobStage.finalize)


def _add_done_gate_jobs(db_session, doc_id, *, exclude: set[JobStage] | None = None):
    exclude = exclude or set()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=doc_id,
                stage=stage,
                status=JobStatus.done,
            )
            for stage in _READY_GATE_STAGES
            if stage not in exclude
        ]
    )


async def test_setup_status_no_users(client):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is True


async def test_setup_status_with_users(client, admin_user):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is False


async def test_ping_liveness_is_unauthenticated_and_dependency_free(client):
    """The liveness probe returns 200 {"status": "ok"} with no auth and without
    touching Postgres/Tika/storage — the macOS HealthChecker relies on this to
    distinguish a zombied API listener from a degraded backend."""
    resp = await client.get("/api/system/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_check(client):
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["checks"]["postgres"] == "ok"


async def test_health_check_reports_embedder(client, monkeypatch):
    import httpx

    real_get = httpx.AsyncClient.get

    async def _selective_get(self, url, *args, **kwargs):
        if str(url) == "http://embedder:8000/health":
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", str(url)))
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _selective_get)

    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["checks"]["embedder"] == "ok"


async def test_health_check_exposes_allow_source_download_default_false(client):
    """The health endpoint surfaces the allow_source_download capability so the
    frontend can decide whether to render the Download button. Default must be
    False on every deployment — see the setting docstring for why."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "allow_source_download" in data
    assert data["allow_source_download"] is False


async def test_health_check_exposes_enable_cli_access_default_false(client):
    """The health endpoint surfaces the enable_cli_access capability so the
    frontend Integrations page can show whether CLI access is enabled. Default
    must be False on every deployment — it is an opt-in feature."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "enable_cli_access" in data
    assert data["enable_cli_access"] is False


async def test_health_check_reflects_allow_source_download_when_set(client):
    """Toggling the setting at runtime should be visible immediately on the
    next health fetch. Tests use the same Settings singleton; mutating it on
    the fly is the production-equivalent of an admin flipping the env var
    and restarting (the singleton is read fresh on each request)."""
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.allow_source_download
    s.allow_source_download = True
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        assert resp.json()["allow_source_download"] is True
    finally:
        s.allow_source_download = original


async def test_health_check_cli_shim_absent_on_docker(client):
    """On Docker (native_config_file not set), cli_shim_install_status must be
    None so the frontend omits the macOS-only shim section."""
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    s.native_config_file = ""  # simulate Docker
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("cli_shim_install_status") is None
    finally:
        s.native_config_file = original


async def test_status_summary_surfaces_recovery_attention(client, admin_user, admin_token, db_session):
    now = datetime.now(UTC)
    failed_doc = Document(
        title="Broken scan",
        status="active",
        sha256=b"f" * 32,
        pipeline_status=PipelineStatus.error,
        error="Tika failed",
    )
    processing_doc = Document(
        title="Still embedding",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.embedding,
    )
    ner_doc = Document(
        title="No entities",
        status="active",
        sha256=b"n" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add_all([failed_doc, processing_doc, ner_doc])
    await db_session.flush()

    db_session.add_all(
        [
            IngestionJob(
                doc_id=failed_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.error,
                error="Tika failed",
            ),
            IngestionJob(
                doc_id=processing_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
                started_at=now - timedelta(hours=3),
            ),
            IngestionJob(
                doc_id=ner_doc.doc_id,
                stage=JobStage.entities,
                status=JobStatus.done,
                metrics={"skipped": True, "reason": "spacy_unavailable"},
            ),
        ]
    )
    db_session.add(WatchedFolder(path="/missing", unavailable_reason="permission denied"))
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 1
    assert data["counts"]["processing_documents"] == 1
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["stuck_jobs"] == 1
    assert data["counts"]["unavailable_folders"] == 1
    assert data["counts"]["ner_skipped_documents"] == 1

    issue_kinds = {issue["kind"] for issue in data["needs_attention"]}
    assert {
        "failed_documents",
        "stuck_jobs",
        "folder_access",
        "entity_extraction_skipped",
    }.issubset(issue_kinds)
    entity_issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "entity_extraction_skipped")
    assert entity_issue["title"] == "Entity extraction skipped some documents"
    assert "open Maintenance and reprocess" in entity_issue["detail"]
    assert entity_issue["action_label"] == "Open maintenance"
    assert entity_issue["action_href"] == "/settings/maintenance"
    assert data["recent_failed_documents"][0]["title"] == "Broken scan"
    assert data["recent_failed_documents"][0]["failed_stage"] == "extract"
    assert data["recent_processing_documents"][0]["title"] == "Still embedding"
    assert data["recent_processing_documents"][0]["processing_stage"] == "embed"
    assert data["recent_processing_documents"][0]["job_status"] == "running"


async def test_status_summary_surfaces_failed_summarize_jobs(client, admin_user, admin_token, db_session):
    doc = Document(
        title="Searchable but unsummarized",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.error,
            error="AppleIntelligenceUnavailableError: Apple Intelligence summaries are enabled but unavailable",
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_jobs"] == 1
    assert data["counts"]["failed_summarize_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "summary_generation_failed")
    assert issue["severity"] == "warning"
    assert issue["title"] == "Summaries failed to generate"
    assert issue["count"] == 1
    assert "Documents remain searchable" in issue["detail"]
    assert issue["action_href"] == "/settings/maintenance"


async def test_status_summary_surfaces_blocked_summarize_jobs(client, admin_user, admin_token, db_session):
    retry_at = datetime.now(UTC) + timedelta(minutes=10)
    doc = Document(
        title="Waiting on Apple Intelligence",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.queued,
            pipeline_seq=doc.pipeline_seq,
            metrics={
                "blocked": True,
                "reason": "apple_intelligence_unavailable",
                "retry_attempts": 2,
                "retry_after": retry_at.isoformat(),
            },
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_summarize_jobs"] == 0
    assert data["counts"]["blocked_summarize_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "summary_generation_blocked")
    assert issue["severity"] == "warning"
    assert issue["title"] == "Apple Intelligence summaries are paused"
    assert issue["count"] == 1
    assert "retry automatically with backoff" in issue["detail"]
    assert issue["action_href"] == "/settings/models"


async def test_status_summary_separates_stranded_pipeline_state(client, admin_user, admin_token, db_session):
    stranded_doc = Document(
        title="Marked chunking",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add(stranded_doc)
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 1
    assert data["recent_processing_documents"] == []

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "stranded_pipeline_state")
    assert issue["severity"] == "warning"
    assert issue["count"] == 1
    assert "no queued or running ingest job" in issue["detail"]


async def test_status_summary_surfaces_completed_status_cleanup(client, admin_user, admin_token, db_session):
    stale_doc = Document(
        title="Finished but marked queued",
        status="active",
        sha256=b"q" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(stale_doc)
    await db_session.flush()
    _add_done_gate_jobs(db_session, stale_doc.doc_id)
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["ready_documents"] == 1
    assert data["counts"]["stored_ready_documents"] == 0
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["completed_status_stale_documents"] == 1
    assert data["recent_processing_documents"] == []

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "completed_status_stale")
    assert issue["severity"] == "warning"
    assert issue["count"] == 1
    assert issue["action_label"] == "Repair statuses"
    assert issue["action_kind"] == "repair_completed_statuses"
    assert "completed ingest" in issue["detail"]


async def test_status_summary_reports_summarize_backlog_separately(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready but summarizing",
        status="active",
        sha256=b"z" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
    )
    db_session.add(ready_doc)
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=ready_doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.queued,
            pipeline_seq=ready_doc.pipeline_seq,
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "processing"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["summarizing_documents"] == 1
    assert data["counts"]["queued_jobs"] == 0
    assert data["counts"]["running_jobs"] == 0
    assert data["counts"]["summarizing_queued_jobs"] == 1
    assert data["counts"]["summarizing_running_jobs"] == 0
    assert data["counts"]["total_queued_jobs"] == 1
    assert data["recent_processing_documents"] == []
    assert data["needs_attention"] == []


async def test_system_stats_reports_worker_queue_rollups(client, admin_user, admin_token, db_session):
    io_doc = Document(
        title="IO queued",
        status="active",
        sha256=b"i" * 32,
        pipeline_status=PipelineStatus.extracting,
        pipeline_seq=1,
    )
    cpu_doc = Document(
        title="CPU running",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.embedding,
        pipeline_seq=1,
    )
    llm_doc = Document(
        title="LLM queued",
        status="active",
        sha256=b"l" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    stale_doc = Document(
        title="Stale job",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.chunking,
        pipeline_seq=2,
    )
    inactive_doc = Document(
        title="Inactive job",
        status="inactive",
        sha256=b"x" * 32,
        pipeline_status=PipelineStatus.ocr_running,
        pipeline_seq=1,
    )
    db_session.add_all([io_doc, cpu_doc, llm_doc, stale_doc, inactive_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=io_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.queued,
                pipeline_seq=io_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=cpu_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
                pipeline_seq=cpu_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=llm_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=llm_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=stale_doc.doc_id,
                stage=JobStage.chunk,
                status=JobStatus.queued,
                pipeline_seq=1,
            ),
            IngestionJob(
                doc_id=inactive_doc.doc_id,
                stage=JobStage.ocr,
                status=JobStatus.queued,
                pipeline_seq=inactive_doc.pipeline_seq,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.get("/api/system/stats", headers=auth_header(admin_token))
    assert resp.status_code == 200
    queues = resp.json()["queues"]
    assert queues == {
        "io_queued": 1,
        "io_running": 0,
        "cpu_queued": 0,
        "cpu_running": 1,
        "llm_queued": 1,
        "llm_running": 0,
    }


async def test_status_summary_summarize_jobs_do_not_hide_completed_status_cleanup(
    client, admin_user, admin_token, db_session
):
    stale_doc = Document(
        title="Foreground complete with background summary",
        status="active",
        sha256=b"u" * 32,
        pipeline_status=PipelineStatus.finalizing,
        pipeline_seq=0,
    )
    db_session.add(stale_doc)
    await db_session.flush()
    _add_done_gate_jobs(db_session, stale_doc.doc_id)
    db_session.add(
        IngestionJob(
            doc_id=stale_doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.running,
            pipeline_seq=stale_doc.pipeline_seq,
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["summarizing_documents"] == 1
    assert data["counts"]["completed_status_stale_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["running_jobs"] == 0
    assert data["counts"]["summarizing_running_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "completed_status_stale")
    assert issue["count"] == 1


async def test_repair_completed_statuses_marks_finalized_stale_docs_ready(client, admin_user, admin_token, db_session):
    repairable_doc = Document(
        title="Repairable",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.chunking,
        error="old transient state",
    )
    still_running_doc = Document(
        title="Still running",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    unfinished_doc = Document(
        title="Unfinished",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    finalize_only_doc = Document(
        title="Finalize row only",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add_all([repairable_doc, still_running_doc, unfinished_doc, finalize_only_doc])
    await db_session.flush()
    _add_done_gate_jobs(db_session, repairable_doc.doc_id)
    _add_done_gate_jobs(db_session, still_running_doc.doc_id, exclude={JobStage.embed})
    db_session.add_all(
        [
            IngestionJob(
                doc_id=still_running_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
            ),
            IngestionJob(
                doc_id=finalize_only_doc.doc_id,
                stage=JobStage.finalize,
                status=JobStatus.done,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/repair-completed-statuses", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"repaired": 1}

    await db_session.refresh(repairable_doc)
    await db_session.refresh(still_running_doc)
    await db_session.refresh(unfinished_doc)
    await db_session.refresh(finalize_only_doc)
    assert repairable_doc.pipeline_status == PipelineStatus.ready
    assert repairable_doc.error is None
    assert still_running_doc.pipeline_status == PipelineStatus.chunking
    assert unfinished_doc.pipeline_status == PipelineStatus.chunking
    assert finalize_only_doc.pipeline_status == PipelineStatus.chunking


async def test_reprocess_all_bulk_resets_active_docs_and_queues_extract(client, admin_user, admin_token, db_session):
    active_doc = Document(
        title="Active",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=4,
        summary="Existing summary that should be regenerated by a full reprocess.",
        error="old failure",
    )
    inactive_doc = Document(
        title="Inactive",
        status="inactive",
        sha256=b"i" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=7,
    )
    db_session.add_all([active_doc, inactive_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.done,
                metrics={"old": True},
            ),
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.chunk,
                status=JobStatus.done,
            ),
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.done,
                pipeline_seq=4,
                metrics={"skipped": True, "reason": "old_cleanup"},
            ),
            IngestionJob(
                doc_id=inactive_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.done,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/reprocess-all", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"reprocessed": 1}

    await db_session.refresh(active_doc)
    await db_session.refresh(inactive_doc)
    assert active_doc.pipeline_status == PipelineStatus.extracting
    assert active_doc.pipeline_seq == 5
    assert active_doc.error is None
    assert inactive_doc.pipeline_status == PipelineStatus.ready
    assert inactive_doc.pipeline_seq == 7

    active_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == active_doc.doc_id))).scalars().all()
    )
    assert len(active_jobs) == 1
    assert active_jobs[0].stage == JobStage.extract
    assert active_jobs[0].status == JobStatus.queued
    assert active_jobs[0].metrics == {}
    assert active_jobs[0].started_at is None
    assert active_jobs[0].finished_at is None

    inactive_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == inactive_doc.doc_id)))
        .scalars()
        .all()
    )
    assert len(inactive_jobs) == 1
    assert inactive_jobs[0].stage == JobStage.extract
    assert inactive_jobs[0].status == JobStatus.done


async def test_resummarize_all_bulk_upserts_ready_docs_only(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
        summary="Existing summary that should be regenerated by explicit resummarize.",
        error="old summary error",
    )
    processing_doc = Document(
        title="Processing",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.chunking,
        pipeline_seq=1,
    )
    stale_queued_doc = Document(
        title="Stale queued",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
    )
    db_session.add_all([ready_doc, processing_doc, stale_queued_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=ready_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.done,
                metrics={"skipped": True},
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            ),
            IngestionJob(
                doc_id=stale_queued_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
                metrics={"old": True},
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/resummarize-all", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"resummarized": 2}

    await db_session.refresh(ready_doc)
    await db_session.refresh(processing_doc)
    assert ready_doc.pipeline_status == PipelineStatus.ready
    assert ready_doc.error is None
    assert processing_doc.pipeline_status == PipelineStatus.chunking

    ready_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == ready_doc.doc_id))).scalars().all()
    )
    assert len(ready_jobs) == 1
    summarize_job = ready_jobs[0]
    assert summarize_job.stage == JobStage.summarize
    assert summarize_job.status == JobStatus.queued
    assert summarize_job.pipeline_seq == ready_doc.pipeline_seq
    assert summarize_job.metrics == {}
    assert summarize_job.started_at is None
    assert summarize_job.finished_at is None

    stale_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == stale_queued_doc.doc_id)))
        .scalars()
        .all()
    )
    assert len(stale_jobs) == 1
    stale_job = stale_jobs[0]
    assert stale_job.stage == JobStage.summarize
    assert stale_job.status == JobStatus.queued
    assert stale_job.pipeline_seq == stale_queued_doc.pipeline_seq
    assert stale_job.metrics == {}

    processing_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == processing_doc.doc_id)))
        .scalars()
        .all()
    )
    assert processing_jobs == []


async def test_clear_redundant_summary_backlog_only_skips_safe_queued_jobs(client, admin_user, admin_token, db_session):
    with_summary = Document(
        title="Has summary",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    no_summary = Document(
        title="Missing summary",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary=None,
    )
    running = Document(
        title="Running summary",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    stale_generation = Document(
        title="Stale generation",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
        summary="Already summarized.",
    )
    inactive = Document(
        title="Inactive",
        status="inactive",
        sha256=b"e" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    db_session.add_all([with_summary, no_summary, running, stale_generation, inactive])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=with_summary.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
                metrics={"existing": True},
            ),
            IngestionJob(
                doc_id=no_summary.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=running.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.running,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=stale_generation.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=inactive.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/clear-redundant-summary-backlog", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1}

    jobs = (
        (
            await db_session.execute(
                select(IngestionJob).where(
                    IngestionJob.stage == JobStage.summarize,
                )
            )
        )
        .scalars()
        .all()
    )
    by_doc = {job.doc_id: job for job in jobs}

    cleared = by_doc[with_summary.doc_id]
    assert cleared.status == JobStatus.done
    assert cleared.started_at is not None
    assert cleared.finished_at is not None
    assert cleared.heartbeat_at is None
    assert cleared.error is None
    assert cleared.metrics["existing"] is True
    assert cleared.metrics["skipped"] is True
    assert cleared.metrics["reason"] == "existing_summary_cleanup"

    assert by_doc[no_summary.doc_id].status == JobStatus.queued
    assert by_doc[running.doc_id].status == JobStatus.running
    assert by_doc[stale_generation.doc_id].status == JobStatus.queued
    assert by_doc[inactive.doc_id].status == JobStatus.queued


async def test_status_summary_ready_when_no_attention_items(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    deleted_failed_doc = Document(
        title="Deleted failure",
        status="deleted",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.error,
    )
    db_session.add_all([ready_doc, deleted_failed_doc])
    db_session.add(WatchedFolder(path="/docs"))
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=deleted_failed_doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.error,
            error="old failure",
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["counts"]["ready_documents"] == 1
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_jobs"] == 0
    assert data["needs_attention"] == []


# ---------------------------------------------------------------------------
# Unit tests for _cli_shim_install_status() using tmp_path.
# We pass _shim= directly to avoid touching ~/.local/bin on the test machine.
# ---------------------------------------------------------------------------

_SHIM_MARKER = "# harbor-clerk — installed by Harbor Clerk Server"


def _make_shim(bundle: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{_SHIM_MARKER}\n"
        f'BUNDLE_RESOURCES="{bundle}"\n'
        'exec "$BUNDLE_RESOURCES/venv/bin/python" -m harbor_clerk.cli.main "$@"\n'
    )


def test_cli_shim_not_installed_when_file_absent(tmp_path):
    """When the shim file doesn't exist, return 'not_installed'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)
    try:
        absent = tmp_path / "harbor-clerk"  # does not exist
        assert _cli_shim_install_status(_shim=absent) == "not_installed"
    finally:
        s.native_config_file = original


def test_cli_shim_installed_when_bundle_matches(tmp_path, monkeypatch):
    """When shim bundle matches BUNDLE_RESOURCES env var, return 'installed'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    fake_bundle = "/fake/Harbor Clerk Server.app/Contents/Resources"
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim(fake_bundle))
    monkeypatch.setenv("BUNDLE_RESOURCES", fake_bundle)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed"
    finally:
        s.native_config_file = original


def test_cli_shim_installed_outdated_when_bundle_differs(tmp_path, monkeypatch):
    """When shim bundle doesn't match BUNDLE_RESOURCES, return 'installed_outdated'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    stale_bundle = "/old/Harbor Clerk Server.app/Contents/Resources"
    current_bundle = "/new/Harbor Clerk Server.app/Contents/Resources"
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim(stale_bundle))
    monkeypatch.setenv("BUNDLE_RESOURCES", current_bundle)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed_outdated"
    finally:
        s.native_config_file = original


def test_cli_shim_foreign_script_treated_as_not_installed(tmp_path):
    """A harbor-clerk script without our marker must be left alone (not_installed)."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    foreign = '#!/bin/sh\n# installed by homebrew\nexec /opt/homebrew/bin/hc "$@"\n'
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(foreign)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "not_installed"
    finally:
        s.native_config_file = original


def test_cli_shim_no_bundle_resources_env_treated_as_installed(tmp_path, monkeypatch):
    """When BUNDLE_RESOURCES env var is absent (unusual), treat as installed
    rather than reporting false outdated status."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    monkeypatch.delenv("BUNDLE_RESOURCES", raising=False)
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim("/some/bundle"))
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed"
    finally:
        s.native_config_file = original


async def test_summary_backlog_endpoint_returns_all_four_fields(client, admin_user, admin_token):
    """The Observatory Summary Backlog widget needs depth, throughput,
    p50, and depth-over-time history. Endpoint must return all four."""
    response = await client.get("/api/system/summary-backlog", headers=auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()
    assert "queue_depth" in data
    assert "throughput_per_min" in data
    assert "p50_seconds" in data
    assert "depth_history" in data
    assert isinstance(data["depth_history"], list)
    if data["depth_history"]:
        assert len(data["depth_history"][0]) == 2
    # 5-minute samples over the last hour = 13 buckets
    assert len(data["depth_history"]) == 13
    # Type + range checks: regressions like queue_depth=null, p50="N/A",
    # or throughput as a string would all pass mere presence checks.
    assert isinstance(data["queue_depth"], int) and data["queue_depth"] >= 0
    assert isinstance(data["throughput_per_min"], (int, float)) and data["throughput_per_min"] >= 0
    assert isinstance(data["p50_seconds"], (int, float)) and data["p50_seconds"] >= 0
    assert all(isinstance(ts, (int, float)) for ts, _ in data["depth_history"])
    assert all(isinstance(d, int) and d >= 0 for _, d in data["depth_history"])
