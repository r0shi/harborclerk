"""Tests for /api/system/* endpoints."""

from datetime import UTC, datetime, timedelta

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFolder
from tests.conftest import auth_header


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
