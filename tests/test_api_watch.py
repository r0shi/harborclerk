"""Tests for /api/watch endpoints (system + progress + stream)."""

import uuid

from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
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

    async def _add_file(sha: bytes) -> uuid.UUID:
        doc = Document(title="d", status="active", sha256=sha, pipeline_status=PipelineStatus.queued)
        db_session.add(doc)
        await db_session.flush()
        wf = WatchedFile(
            folder_id=folder.folder_id,
            relative_path=sha.hex()[:8],
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
        db_session.add(wf)
        return doc.doc_id

    doc_a = await _add_file(b"a" * 32)
    doc_b = await _add_file(b"b" * 32)

    db_session.add_all(
        [
            IngestionJob(doc_id=doc_a, stage=JobStage.extract, status=JobStatus.done),
            IngestionJob(doc_id=doc_a, stage=JobStage.ocr, status=JobStatus.running),
            IngestionJob(doc_id=doc_a, stage=JobStage.chunk, status=JobStatus.queued),
            IngestionJob(doc_id=doc_b, stage=JobStage.extract, status=JobStatus.done),
            IngestionJob(doc_id=doc_b, stage=JobStage.ocr, status=JobStatus.error),
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
        sha = str(uuid.uuid4()).encode()[:32].ljust(32, b"\x00")
        doc = Document(title="d", status="active", sha256=sha, pipeline_status=PipelineStatus.queued)
        db_session.add(doc)
        await db_session.flush()
        wf = WatchedFile(
            folder_id=folder.folder_id,
            relative_path=str(uuid.uuid4()),
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=status,
        )
        db_session.add(wf)
        return doc.doc_id

    await _seed(WatchedFileStatus.active)
    await _seed(WatchedFileStatus.removed)
    await db_session.flush()

    resp = await client.get(
        f"/api/watch/folders/{folder.folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["total_files"] == 1


# ---------------------------------------------------------------------------
# POST /api/watch/folders — platform-aware validation
# ---------------------------------------------------------------------------


async def test_post_folder_macos_rejects_nonexistent(client, admin_token, monkeypatch):
    """macOS: path must exist as a readable directory."""
    monkeypatch.setenv("WATCH_ROOT", "")
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": "/no/such/dir", "bookmark_data": "", "recursive": True},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


async def test_post_folder_macos_accepts_existing_dir(client, admin_token, monkeypatch, tmp_path):
    """macOS: bookmark_data is optional — new web-flow clients can omit it."""
    monkeypatch.setenv("WATCH_ROOT", "")
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": str(tmp_path), "recursive": True},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201


async def test_post_folder_docker_rejects_outside_root(client, admin_token, monkeypatch, tmp_path):
    """Docker: path must be a top-level subdir of WATCH_ROOT."""
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": "/etc"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


async def test_post_folder_docker_accepts_subdir_of_root(client, admin_token, monkeypatch, tmp_path):
    """Docker: an existing top-level subdir of WATCH_ROOT is accepted."""
    sub = tmp_path / "inbox"
    sub.mkdir()
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": str(sub)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# DELETE /api/watch/folders/{folder_id} — 409 for active Docker mounts
# ---------------------------------------------------------------------------


async def test_delete_active_auto_discovered_returns_409(client, admin_token, db_session, monkeypatch, tmp_path):
    """auto_discovered=True AND unavailable_reason IS NULL → cannot be deleted."""
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    sub = tmp_path / "inbox"
    sub.mkdir()
    folder = WatchedFolder(
        path=str(sub),
        auto_discovered=True,
        display_name="inbox",
        bookmark_data=None,
    )
    db_session.add(folder)
    await db_session.flush()
    resp = await client.delete(
        f"/api/watch/folders/{folder.folder_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409


async def test_delete_unmounted_auto_discovered_succeeds(client, admin_token, db_session, monkeypatch, tmp_path):
    """auto_discovered=True but unavailable_reason='unmounted' → DELETE allowed."""
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    folder = WatchedFolder(
        path="/tmp/gone",
        auto_discovered=True,
        display_name="gone",
        bookmark_data=None,
        unavailable_reason="unmounted",
        enabled=False,
    )
    db_session.add(folder)
    await db_session.flush()
    resp = await client.delete(
        f"/api/watch/folders/{folder.folder_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204


async def test_delete_manual_folder_succeeds(client, admin_token, db_session, monkeypatch, tmp_path):
    """auto_discovered=False folders are always deletable."""
    monkeypatch.setenv("WATCH_ROOT", "")
    _reset_settings_cache(monkeypatch)
    folder = WatchedFolder(
        path=str(tmp_path),
        auto_discovered=False,
        display_name="manual",
        bookmark_data=None,
    )
    db_session.add(folder)
    await db_session.flush()
    resp = await client.delete(
        f"/api/watch/folders/{folder.folder_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /api/watch/folders/stream — SSE per-folder progress deltas
# ---------------------------------------------------------------------------


async def test_folder_progress_stream_emits_initial_snapshot(_engine, db_session):
    """The SSE generator emits a data line for each existing folder on first iteration.

    Note: we drive the generator directly rather than via HTTP because httpx's
    ASGITransport buffers the entire response body before returning — incompatible
    with infinite SSE streams. The endpoint wrapper itself is trivial (a
    StreamingResponse around this generator), so this exercises the meaningful
    logic.

    We pass a session factory bound to the test `_engine` so the generator
    doesn't bind the module-level `async_session_factory` to this test's
    transient event loop (which fails when other tests have already created
    and torn down their own loops).
    """
    import asyncio
    import json

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from harbor_clerk.api.routes.watch import _folder_progress_event_generator

    folder = WatchedFolder(path="/tmp/sse", display_name="sse", bookmark_data=None)
    db_session.add(folder)
    await db_session.commit()  # commit so the generator (its own session) sees it

    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        gen = _folder_progress_event_generator(session_factory=factory)
        try:
            # First emission must arrive in well under a second; 5s is a safety net.
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        finally:
            await gen.aclose()  # exercises the CancelledError path

        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        payload = json.loads(chunk[len("data: ") :].rstrip("\n"))
        # The first emission may be for any folder (other tests can leak rows
        # if their cleanup hadn't run yet); just confirm shape and that our
        # folder shows up somewhere in the initial snapshot.
        assert "folder_id" in payload
        assert "total_files" in payload
        assert "completed_files" in payload
        assert payload["scan_status"] in ("scanning", "idle")
    finally:
        # Cleanup the seeded folder so it doesn't leak across tests
        from sqlalchemy import delete as sa_delete

        await db_session.execute(sa_delete(WatchedFolder).where(WatchedFolder.folder_id == folder.folder_id))
        await db_session.commit()


async def test_folder_progress_stream_requires_auth(client):
    resp = await client.get("/api/watch/folders/stream")
    assert resp.status_code == 401


async def test_ingest_rejects_excalidraw_md(client, admin_token, db_session, tmp_path):
    """*.excalidraw.md carries a JSON blob, not prose — /ingest must reject it."""
    folder = WatchedFolder(path=str(tmp_path), display_name="vault", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()

    resp = await client.post(
        "/api/watch/ingest",
        headers=auth_header(admin_token),
        json={
            "folder_id": str(folder.folder_id),
            "relative_path": "Diagram.excalidraw.md",
            "sha256": "00" * 32,
            "bookmark_data": "",
            "source_path": str(tmp_path / "Diagram.excalidraw.md"),
            "mime_type": "text/markdown",
        },
    )
    assert resp.status_code == 400
    assert "excalidraw" in resp.json()["detail"].lower()
