"""Tests for /api/watch endpoints (system + progress + stream)."""

import uuid

from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import JobStage, JobStatus, VersionStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from tests.conftest import auth_header


def _reset_settings_cache(monkeypatch) -> None:
    """Drop the module-level Settings singleton so the next get_settings() re-reads env."""
    import harbor_clerk.config as config

    monkeypatch.setattr(config, "_settings", None)


async def test_get_watch_system_macos(client, admin_token, monkeypatch):
    """Empty WATCH_ROOT (macOS native deployment) returns native picker."""
    monkeypatch.setenv("WATCH_ROOT", "")
    _reset_settings_cache(monkeypatch)

    resp = await client.get("/api/watch/system", headers=auth_header(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "macos"
    assert body["picker"] == "native"
    assert body["watch_root"] is None


async def test_get_watch_system_docker(client, admin_token, monkeypatch):
    """Non-empty WATCH_ROOT (Docker deployment) returns no-picker + the root path."""
    monkeypatch.setenv("WATCH_ROOT", "/data/watch")
    _reset_settings_cache(monkeypatch)

    resp = await client.get("/api/watch/system", headers=auth_header(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "docker"
    assert body["picker"] == "none"
    assert body["watch_root"] == "/data/watch"


async def test_get_watch_system_requires_auth(client):
    resp = await client.get("/api/watch/system")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/watch/folders/{folder_id}/progress
# ---------------------------------------------------------------------------


async def _seed_folder_with_jobs(db_session):
    """Seed: one folder, two active WatchedFiles, IngestionJobs in different states.

    File A: extract done, ocr running, chunk queued.
    File B: extract done, ocr error.
    Returns folder_id.
    """
    folder = WatchedFolder(path="/tmp/x", display_name="x", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()

    async def _add_file(content: bytes):
        doc = Document(title="d", status="active")
        db_session.add(doc)
        await db_session.flush()
        version = DocumentVersion(doc_id=doc.doc_id, original_sha256=content, status=VersionStatus.queued)
        db_session.add(version)
        await db_session.flush()
        wf = WatchedFile(
            folder_id=folder.folder_id,
            relative_path=content.decode("ascii", errors="ignore") or "f",
            bookmark_data=b"",
            sha256=content,
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status=WatchedFileStatus.active,
        )
        db_session.add(wf)
        return version.version_id

    v_a = await _add_file(b"a" * 32)
    v_b = await _add_file(b"b" * 32)

    db_session.add_all(
        [
            IngestionJob(version_id=v_a, stage=JobStage.extract, status=JobStatus.done),
            IngestionJob(version_id=v_a, stage=JobStage.ocr, status=JobStatus.running),
            IngestionJob(version_id=v_a, stage=JobStage.chunk, status=JobStatus.queued),
            IngestionJob(version_id=v_b, stage=JobStage.extract, status=JobStatus.done),
            IngestionJob(version_id=v_b, stage=JobStage.ocr, status=JobStatus.error),
        ]
    )
    await db_session.flush()
    return folder.folder_id


async def test_folder_progress_aggregates(client, admin_token, db_session):
    folder_id = await _seed_folder_with_jobs(db_session)
    resp = await client.get(
        f"/api/watch/folders/{folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_files"] == 2
    assert body["completed_files"] == 0  # no finalize jobs in seed
    assert "by_stage" in body
    for stage in ("extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize"):
        assert stage in body["by_stage"]
        for state in ("pending", "running", "done", "error"):
            assert state in body["by_stage"][stage]
    assert body["by_stage"]["extract"]["done"] == 2
    assert body["by_stage"]["ocr"]["running"] == 1
    assert body["by_stage"]["ocr"]["error"] == 1
    assert body["by_stage"]["chunk"]["pending"] == 1
    assert body["scan_status"] == "scanning"  # last_scan_at is null


async def test_folder_progress_404_for_unknown(client, admin_token):
    resp = await client.get(
        f"/api/watch/folders/{uuid.uuid4()}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


async def test_folder_progress_empty_folder(client, admin_token, db_session):
    folder = WatchedFolder(path="/tmp/empty", display_name="empty", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()
    resp = await client.get(
        f"/api/watch/folders/{folder.folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_files"] == 0
    assert body["completed_files"] == 0
    # All 7 stages × 4 states should still be present, all zero
    for stage in ("extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize"):
        assert body["by_stage"][stage] == {"pending": 0, "running": 0, "done": 0, "error": 0}


async def test_folder_progress_scan_status_idle(client, admin_token, db_session):
    """When last_scan_at is set, scan_status is 'idle'."""
    from datetime import UTC, datetime

    folder = WatchedFolder(
        path="/tmp/scanned",
        display_name="scanned",
        bookmark_data=None,
        last_scan_at=datetime.now(UTC),
    )
    db_session.add(folder)
    await db_session.flush()
    resp = await client.get(
        f"/api/watch/folders/{folder.folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_status"] == "idle"
    assert body["last_scan_at"] is not None


async def test_folder_progress_only_counts_active_files(client, admin_token, db_session):
    """WatchedFile rows with status=removed are excluded from total_files."""
    folder = WatchedFolder(path="/tmp/y", display_name="y", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()

    async def _seed(status: WatchedFileStatus):
        doc = Document(title="d", status="active")
        db_session.add(doc)
        await db_session.flush()
        ver = DocumentVersion(
            doc_id=doc.doc_id,
            original_sha256=str(uuid.uuid4()).encode(),
            status=VersionStatus.queued,
        )
        db_session.add(ver)
        await db_session.flush()
        wf = WatchedFile(
            folder_id=folder.folder_id,
            relative_path=str(uuid.uuid4()),
            bookmark_data=b"",
            sha256=str(uuid.uuid4()).encode(),
            doc_id=doc.doc_id,
            version_id=ver.version_id,
            status=status,
        )
        db_session.add(wf)
        return ver.version_id

    await _seed(WatchedFileStatus.active)
    await _seed(WatchedFileStatus.removed)
    await db_session.flush()

    resp = await client.get(
        f"/api/watch/folders/{folder.folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["total_files"] == 1
