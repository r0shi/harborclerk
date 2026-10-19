"""Tests for /api/watch endpoints (system + progress + stream)."""

import uuid

import pytest
from sqlalchemy import func, select

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
    # File A has ocr.running + chunk.queued — in-flight count > 0 → processing.
    assert body["ingest_status"] == "processing"


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
    # No jobs means nothing in flight.
    assert body["ingest_status"] == "idle"


async def test_folder_progress_ingest_status_idle_when_only_done_or_error(client, admin_token, db_session):
    """Folder with finished + errored jobs only (no pending/running) reports idle."""
    folder = WatchedFolder(path="/tmp/done", display_name="done", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()
    sha = b"d" * 32
    doc = Document(title="d", status="active", sha256=sha, pipeline_status=PipelineStatus.queued)
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path="d",
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    db_session.add_all(
        [
            IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.done),
            IngestionJob(doc_id=doc.doc_id, stage=JobStage.ocr, status=JobStatus.error),
            IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.done),
        ]
    )
    await db_session.flush()

    resp = await client.get(
        f"/api/watch/folders/{folder.folder_id}/progress",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ingest_status"] == "idle"


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


async def test_post_folder_docker_refuses_outside_root(client, admin_token, db_session, monkeypatch, tmp_path):
    """Docker: no folder is added by hand, inside WATCH_ROOT or out."""
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": "/etc"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert (await db_session.execute(select(func.count()).select_from(WatchedFolder))).scalar_one() == 0


async def test_post_folder_docker_refuses_subdir_of_root(client, admin_token, db_session, monkeypatch, tmp_path):
    """Docker: a subdirectory of WATCH_ROOT is discovered, never POSTed.

    Accepting it created a manual row the discovery loop then duplicated with an
    auto-discovered one: two observers, two documents per file (#870 review).
    """
    sub = tmp_path / "inbox"
    sub.mkdir()
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    resp = await client.post(
        "/api/watch/folders",
        json={"path": str(sub)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert "discovered automatically" in resp.json()["detail"]
    assert (await db_session.execute(select(func.count()).select_from(WatchedFolder))).scalar_one() == 0


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
        assert payload["ingest_status"] in ("processing", "idle")
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


# ---------------------------------------------------------------------------
# GET /api/watch/folders — serializer returns skip-tracking fields (Phase 5)
# ---------------------------------------------------------------------------


async def test_folder_serializer_returns_skip_fields(client, admin_token, db_session):
    """GET /api/watch/folders returns skipped_count + skipped_extensions per folder."""
    folder = WatchedFolder(
        path="/tmp/test-skip-serializer",
        display_name="skip-serializer",
        bookmark_data=None,
        skipped_count=4,
        skipped_extensions=[".canvas", ".exe", ".tmp"],
    )
    db_session.add(folder)
    await db_session.flush()

    resp = await client.get("/api/watch/folders", headers=auth_header(admin_token))
    assert resp.status_code == 200
    rows = resp.json()
    by_path = {r["path"]: r for r in rows}
    assert "/tmp/test-skip-serializer" in by_path
    row = by_path["/tmp/test-skip-serializer"]
    assert row["skipped_count"] == 4
    assert row["skipped_extensions"] == [".canvas", ".exe", ".tmp"]


async def test_folder_serializer_skip_fields_defaults(client, admin_token, db_session):
    """A freshly-created folder (no skips tracked yet) returns 0 + []."""
    folder = WatchedFolder(
        path="/tmp/test-skip-defaults-api",
        display_name="skip-defaults",
        bookmark_data=None,
    )
    db_session.add(folder)
    await db_session.flush()

    resp = await client.get("/api/watch/folders", headers=auth_header(admin_token))
    assert resp.status_code == 200
    rows = resp.json()
    by_path = {r["path"]: r for r in rows}
    assert "/tmp/test-skip-defaults-api" in by_path
    row = by_path["/tmp/test-skip-defaults-api"]
    assert row["skipped_count"] == 0
    assert row["skipped_extensions"] == []


# ---------------------------------------------------------------------------
# PATCH /folders/{id} — display_name
# ---------------------------------------------------------------------------


async def test_patch_folder_sets_display_name(client, admin_token, two_folder_corpus):
    """PATCH with display_name='Receipts' updates the row and returns 200."""
    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Receipts"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == "Receipts"


async def test_patch_folder_empty_display_name_reverts_to_basename(client, admin_token, two_folder_corpus):
    """PATCH with display_name='' (or whitespace) writes Path(folder.path).name."""
    from pathlib import Path

    folder_a, _, _, _ = two_folder_corpus

    # First alias to something custom, then clear it
    await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Custom"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": ""},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == Path(folder_a.path).name


async def test_patch_folder_whitespace_display_name_reverts_to_basename(client, admin_token, two_folder_corpus):
    """PATCH with display_name='   ' is treated as empty → reverts."""
    from pathlib import Path

    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "   "},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == Path(folder_a.path).name


async def test_patch_folder_too_long_display_name_returns_422(client, admin_token, two_folder_corpus):
    """display_name longer than 200 chars → 422."""
    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "x" * 201},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


async def test_patch_folder_omitted_display_name_leaves_field_unchanged(client, admin_token, two_folder_corpus):
    """A PATCH that doesn't include display_name must not alter it."""
    folder_a, _, _, _ = two_folder_corpus

    # Set a custom alias first
    await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Sentinel"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # Now PATCH only enabled
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == "Sentinel"


# ---------------------------------------------------------------------------
# /api/watch/ingest and /rename store the canonical source_path
# ---------------------------------------------------------------------------


async def _aliased_folder(db_session, tmp_path):
    """A watched folder stored by its realpath, and a symlink alias to it."""
    real = (tmp_path / "real").resolve()
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    folder = WatchedFolder(path=str(real), display_name="real", bookmark_data=None)
    db_session.add(folder)
    await db_session.commit()
    return str(folder.folder_id), real, link


async def _ingest(client, token, folder_id: str, relative_path: str, source_path: str):
    return await client.post(
        "/api/watch/ingest",
        headers=auth_header(token),
        json={
            "folder_id": folder_id,
            "relative_path": relative_path,
            "sha256": uuid.uuid4().hex * 2,
            "bookmark_data": "",
            "source_path": source_path,
            "mime_type": "text/plain",
        },
    )


async def _source_path_of(db_session, doc_id: str) -> str:
    from sqlalchemy import select

    db_session.expire_all()
    stmt = select(Document.source_path).where(Document.doc_id == uuid.UUID(doc_id))
    return (await db_session.execute(stmt)).scalar_one()


async def test_ingest_stores_the_canonical_path_for_an_aliased_source_path(client, admin_token, db_session, tmp_path):
    """A source_path through a symlinked alias, or with a ./ segment, is stored as folder path + relative path."""
    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    (real / "b.txt").write_text("b")

    via_link = await _ingest(client, admin_token, folder_id, "a.txt", str(link / "a.txt"))
    assert via_link.status_code == 200, via_link.text
    assert await _source_path_of(db_session, via_link.json()["doc_id"]) == str(real / "a.txt")

    dotted = await _ingest(client, admin_token, folder_id, "b.txt", f"{real}/./b.txt")
    assert dotted.status_code == 200, dotted.text
    assert await _source_path_of(db_session, dotted.json()["doc_id"]) == str(real / "b.txt")


async def test_ingest_refuses_a_source_path_naming_another_file(client, admin_token, db_session, tmp_path):
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    (real / "b.txt").write_text("b")

    resp = await _ingest(client, admin_token, folder_id, "a.txt", str(real / "b.txt"))
    assert resp.status_code == 400
    assert "different files" in resp.json()["detail"]


async def test_rename_stores_the_canonical_path_for_an_aliased_source_path(client, admin_token, db_session, tmp_path):
    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    created = await _ingest(client, admin_token, folder_id, "a.txt", str(real / "a.txt"))
    assert created.status_code == 200, created.text
    (real / "a.txt").rename(real / "c.txt")

    resp = await client.post(
        "/api/watch/rename",
        headers=auth_header(admin_token),
        json={
            "folder_id": folder_id,
            "old_relative_path": "a.txt",
            "new_relative_path": "c.txt",
            "bookmark_data": "",
            "source_path": str(link / "c.txt"),
        },
    )
    assert resp.status_code == 200, resp.text
    assert await _source_path_of(db_session, created.json()["doc_id"]) == str(real / "c.txt")


async def test_ingest_of_an_unchanged_file_canonicalizes_an_aliased_source_path(
    client, admin_token, db_session, tmp_path
):
    """The skip branch (same sha) repairs a source_path an older client stored as an alias."""
    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc = Document(
        title="a",
        status="active",
        sha256=bytes.fromhex(sha),
        pipeline_status=PipelineStatus.ready,
        source_path=str(link / "a.txt"),
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=uuid.UUID(folder_id),
            relative_path="a.txt",
            bookmark_data=b"",
            sha256=bytes.fromhex(sha),
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.commit()
    doc_id = str(doc.doc_id)

    resp = await client.post(
        "/api/watch/ingest",
        headers=auth_header(admin_token),
        json={
            "folder_id": folder_id,
            "relative_path": "a.txt",
            "sha256": sha,
            "bookmark_data": "",
            "source_path": str(link / "a.txt"),
            "mime_type": "text/plain",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "skipped"
    assert await _source_path_of(db_session, doc_id) == str(real / "a.txt")


async def test_ingest_refuses_a_relative_path_that_is_not_plain(client, admin_token, db_session, tmp_path):
    """A ./ segment or an absolute relative_path is a 400, even when source_path names the file."""
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")

    dotted = await _ingest(client, admin_token, folder_id, "./a.txt", str(real / "a.txt"))
    assert dotted.status_code == 400, dotted.text
    assert "plain path" in dotted.json()["detail"]

    absolute = await _ingest(client, admin_token, folder_id, str(real / "a.txt"), str(real / "a.txt"))
    assert absolute.status_code == 400, absolute.text
    assert "plain path" in absolute.json()["detail"]


async def test_ingest_refuses_a_symlink_in_the_folder_pointing_outside(client, admin_token, db_session, tmp_path):
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (real / "out.txt").symlink_to(outside)

    via_source = await _ingest(client, admin_token, folder_id, "out.txt", str(real / "out.txt"))
    assert via_source.status_code == 400, via_source.text

    via_relative = await _ingest(client, admin_token, folder_id, "out.txt", str(real / "a.txt"))
    assert via_relative.status_code == 400, via_relative.text


async def _mapped_doc(
    db_session,
    folder_id: str,
    relative_path: str,
    *,
    sha: str,
    source_path: str,
    status: WatchedFileStatus = WatchedFileStatus.active,
    pipeline_status: PipelineStatus = PipelineStatus.ready,
    error: str | None = None,
) -> str:
    """A document mapped to a watched file, as an earlier ingest left it. Returns its doc_id."""
    doc = Document(
        title=relative_path,
        status="active",
        sha256=bytes.fromhex(sha),
        pipeline_status=pipeline_status,
        source_path=source_path,
        error=error,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=uuid.UUID(folder_id),
            relative_path=relative_path,
            bookmark_data=b"",
            sha256=bytes.fromhex(sha),
            doc_id=doc.doc_id,
            status=status,
        )
    )
    await db_session.commit()
    return str(doc.doc_id)


async def _ingest_sha(client, token, folder_id: str, relative_path: str, source_path: str, sha: str):
    return await client.post(
        "/api/watch/ingest",
        headers=auth_header(token),
        json={
            "folder_id": folder_id,
            "relative_path": relative_path,
            "sha256": sha,
            "bookmark_data": "",
            "source_path": source_path,
            "mime_type": "text/plain",
        },
    )


async def _doc_row(db_session, doc_id: str):
    from sqlalchemy import select

    db_session.expire_all()
    return (await db_session.execute(select(Document).where(Document.doc_id == uuid.UUID(doc_id)))).scalar_one()


async def _queued_extracts(db_session, doc_id: str) -> int:
    from sqlalchemy import func, select

    stmt = select(func.count()).where(
        IngestionJob.doc_id == uuid.UUID(doc_id),
        IngestionJob.stage == JobStage.extract,
        IngestionJob.status == JobStatus.queued,
    )
    return (await db_session.execute(stmt)).scalar_one()


@pytest.mark.parametrize("status", [WatchedFileStatus.active, WatchedFileStatus.removed], ids=["case1", "case2"])
async def test_ingest_of_unchanged_bytes_queues_an_errored_doc_again(client, admin_token, db_session, tmp_path, status):
    """Case 1 (active) and Case 2 (removed, resurrected) with the same bytes: a document
    whose last run failed because its file was missing is queued again."""
    from harbor_clerk.error_text import describe_error
    from harbor_clerk.watched_source import WatchedFileMissing

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(
        db_session,
        folder_id,
        "a.txt",
        sha=sha,
        source_path=str(real / "a.txt"),
        status=status,
        pipeline_status=PipelineStatus.error,
        error=describe_error(WatchedFileMissing("the watched file is missing")),
    )

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    # reset_and_queue_extract_for_doc's own status for a queued extract
    assert (await _doc_row(db_session, doc_id)).pipeline_status == PipelineStatus.extracting
    assert await _queued_extracts(db_session, doc_id) == 1


@pytest.mark.parametrize("same_bytes", [True, False], ids=["same-sha", "changed-sha"])
async def test_ingest_resurrect_stores_the_canonical_path(client, admin_token, db_session, tmp_path, same_bytes):
    """Case 2 (removed file resurrected), through an alias: the canonical path is stored."""
    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(
        db_session, folder_id, "a.txt", sha=sha, source_path=str(link / "a.txt"), status=WatchedFileStatus.removed
    )

    sent_sha = sha if same_bytes else uuid.uuid4().hex * 2
    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(link / "a.txt"), sent_sha)

    assert resp.status_code == 200, resp.text
    assert (await _doc_row(db_session, doc_id)).source_path == str(real / "a.txt")


async def test_ingest_of_changed_bytes_stores_the_canonical_path(client, admin_token, db_session, tmp_path):
    """Case 3 (active, changed bytes), through an alias: the canonical path is stored."""
    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path=str(link / "a.txt"))

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(link / "a.txt"), uuid.uuid4().hex * 2)

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "updated"
    assert (await _doc_row(db_session, doc_id)).source_path == str(real / "a.txt")


async def test_ingest_of_a_new_file_records_the_canonical_path_on_its_upload_row(
    client, admin_token, db_session, tmp_path
):
    """Case 4: the Upload row, like the document, carries the canonical path."""
    from sqlalchemy import select

    from harbor_clerk.models.upload import Upload

    folder_id, real, link = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")

    resp = await _ingest(client, admin_token, folder_id, "a.txt", str(link / "a.txt"))

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    upload = (
        await db_session.execute(select(Upload).where(Upload.doc_id == uuid.UUID(resp.json()["doc_id"])))
    ).scalar_one()
    assert upload.source_path == str(real / "a.txt")


async def test_ingest_refuses_a_relative_path_resolving_outside_the_folder(client, admin_token, db_session, tmp_path):
    """Only the containment check refuses this one: source_path is a hard link inside
    the folder to a file outside it, and relative_path a symlink to that same file, so
    the two do name the same file."""
    import os

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    os.link(outside, real / "hard.txt")
    (real / "sym.txt").symlink_to(outside)

    resp = await _ingest(client, admin_token, folder_id, "sym.txt", str(real / "hard.txt"))

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "relative_path is outside the watched folder"


@pytest.mark.parametrize("status", [WatchedFileStatus.active, WatchedFileStatus.removed], ids=["case1", "case2"])
async def test_ingest_of_unchanged_bytes_leaves_a_doc_errored_for_another_reason(
    client, admin_token, db_session, tmp_path, status
):
    """A document that failed for any other reason (here, extraction) is not queued again."""
    from harbor_clerk.error_text import describe_error

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(
        db_session,
        folder_id,
        "a.txt",
        sha=sha,
        source_path=str(real / "a.txt"),
        status=status,
        pipeline_status=PipelineStatus.error,
        error=describe_error(RuntimeError("Tika could not parse the file")),
    )

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    assert (await _doc_row(db_session, doc_id)).pipeline_status == PipelineStatus.error
    assert await _queued_extracts(db_session, doc_id) == 0


async def test_remove_of_a_file_still_present_does_not_remove_it(client, admin_token, db_session, tmp_path):
    """/api/watch/remove for a file still present under that exact name (an atomic
    save's trailing delete) leaves the watched file active and the document readable."""
    from sqlalchemy import select

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path=str(real / "a.txt"))

    resp = await client.post(
        "/api/watch/remove",
        headers=auth_header(admin_token),
        json={"folder_id": folder_id, "relative_path": "a.txt"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "skipped"
    db_session.expire_all()
    wf = (await db_session.execute(select(WatchedFile).where(WatchedFile.relative_path == "a.txt"))).scalar_one()
    assert wf.status == WatchedFileStatus.active
    assert (await _doc_row(db_session, doc_id)).status == "active"


async def test_remove_of_a_file_that_is_gone_removes_it(client, admin_token, db_session, tmp_path):
    from sqlalchemy import select

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "gone.txt", sha=sha, source_path=str(real / "gone.txt"))

    resp = await client.post(
        "/api/watch/remove",
        headers=auth_header(admin_token),
        json={"folder_id": folder_id, "relative_path": "gone.txt"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "removed"
    db_session.expire_all()
    wf = (await db_session.execute(select(WatchedFile).where(WatchedFile.relative_path == "gone.txt"))).scalar_one()
    assert wf.status == WatchedFileStatus.removed
    assert (await _doc_row(db_session, doc_id)).status == "removed"


async def test_remove_of_a_path_now_a_dangling_symlink_removes_it(client, admin_token, db_session, tmp_path):
    """Only a regular file present under that name keeps a /remove from removing."""
    from sqlalchemy import select

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").symlink_to(tmp_path / "nowhere.txt")
    sha = uuid.uuid4().hex * 2
    await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path=str(real / "a.txt"))

    resp = await client.post(
        "/api/watch/remove",
        headers=auth_header(admin_token),
        json={"folder_id": folder_id, "relative_path": "a.txt"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "removed"
    db_session.expire_all()
    wf = (await db_session.execute(select(WatchedFile).where(WatchedFile.relative_path == "a.txt"))).scalar_one()
    assert wf.status == WatchedFileStatus.removed


async def test_ingest_of_unchanged_bytes_does_not_requeue_a_doc_that_does_not_verify(
    client, admin_token, db_session, tmp_path
):
    """Case 1: a document with a missing-source error whose repair is declined (its
    watched path is a directory, not a file) is not queued again."""
    from harbor_clerk.error_text import describe_error
    from harbor_clerk.watched_source import WatchedFileMissing

    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").mkdir()
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(
        db_session,
        folder_id,
        "a.txt",
        sha=sha,
        source_path="a.txt",
        pipeline_status=PipelineStatus.error,
        error=describe_error(WatchedFileMissing("the watched file is missing")),
    )

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    row = await _doc_row(db_session, doc_id)
    assert row.source_path == "a.txt"
    assert row.pipeline_status == PipelineStatus.error
    assert await _queued_extracts(db_session, doc_id) == 0


@pytest.mark.parametrize("label", ["none", "relative", "other-file"])
async def test_ingest_of_an_unchanged_file_heals_a_source_path_that_is_a_label(
    client, admin_token, db_session, tmp_path, label
):
    """Case 1 replaces a label source_path (None, a relative name, another file) with
    the watched file's path when that file is present."""
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "a.txt").write_text("another file")
    stored = {"none": None, "relative": "a.txt", "other-file": str(downloads / "a.txt")}[label]
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path=stored)

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    assert (await _doc_row(db_session, doc_id)).source_path == str(real / "a.txt")


@pytest.mark.parametrize("status", [WatchedFileStatus.active, WatchedFileStatus.removed], ids=["case1", "case2"])
async def test_ingest_of_unchanged_bytes_re_extracts_a_doc_indexed_from_an_older_upload(
    client, admin_token, db_session, tmp_path, status
):
    """Case 1 and Case 2: a document whose sha256 is not its watched file's (a #859-
    shaped new version) takes the file's sha256 and is queued again from the file."""
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path="a.txt", status=status)
    doc = await _doc_row(db_session, doc_id)
    doc.sha256 = bytes.fromhex(uuid.uuid4().hex * 2)  # the older upload's
    await db_session.commit()

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    row = await _doc_row(db_session, doc_id)
    assert row.sha256 == bytes.fromhex(sha)
    assert row.source_path == str(real / "a.txt")
    assert await _queued_extracts(db_session, doc_id) == 1


async def test_ingest_of_unchanged_bytes_does_not_reprocess_a_current_doc(client, admin_token, db_session, tmp_path):
    folder_id, real, _ = await _aliased_folder(db_session, tmp_path)
    (real / "a.txt").write_text("a")
    sha = uuid.uuid4().hex * 2
    doc_id = await _mapped_doc(db_session, folder_id, "a.txt", sha=sha, source_path=str(real / "a.txt"))

    resp = await _ingest_sha(client, admin_token, folder_id, "a.txt", str(real / "a.txt"), sha)

    assert resp.status_code == 200, resp.text
    assert (await _doc_row(db_session, doc_id)).pipeline_status == PipelineStatus.ready
    assert await _queued_extracts(db_session, doc_id) == 0
