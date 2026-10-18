"""Tests for /api/watch endpoints (system + progress + stream)."""

import uuid

import pytest
from sqlalchemy import select

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


@pytest.mark.parametrize("reason", ["root_missing", "root_unreadable", "root_empty"])
async def test_delete_protection_holds_for_the_watchers_informational_reasons(
    client, admin_token, db_session, monkeypatch, tmp_path, reason
):
    """Only discovery's "unmounted" lifts the protection; a pass the watcher
    skipped does not make a live mount deletable."""
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    _reset_settings_cache(monkeypatch)
    sub = tmp_path / "inbox"
    sub.mkdir()
    folder = WatchedFolder(
        path=str(sub), auto_discovered=True, display_name="inbox", bookmark_data=None, unavailable_reason=reason
    )
    db_session.add(folder)
    await db_session.flush()
    resp = await client.delete(f"/api/watch/folders/{folder.folder_id}", headers=auth_header(admin_token))
    assert resp.status_code == 409


@pytest.mark.parametrize(
    ("reason", "refused"),
    [(None, False), ("root_missing", False), ("root_unreadable", False), ("root_empty", False), ("unmounted", True)],
)
async def test_scope_refuses_only_unmounted_folders(db_session, reason, refused):
    """An unplugged drive's documents are kept and searchable, so chat, research
    and search can still scope to its folder; only an unmounted Docker mount is refused."""
    from fastapi import HTTPException

    from harbor_clerk.api.schemas.scope import ScopeSpec
    from harbor_clerk.api.scope_validation import validate_scope_folders

    folder = WatchedFolder(path="/w", display_name="w", bookmark_data=None, unavailable_reason=reason)
    db_session.add(folder)
    await db_session.flush()
    scope = ScopeSpec(folder_ids=[folder.folder_id])
    if refused:
        with pytest.raises(HTTPException) as exc:
            await validate_scope_folders(scope, db_session)
        assert exc.value.status_code == 422
    else:
        await validate_scope_folders(scope, db_session)


def test_only_unmounted_makes_a_folder_unavailable():
    from harbor_clerk.models.watched import WATCHER_UNAVAILABLE_REASONS, folder_is_unavailable

    assert folder_is_unavailable("unmounted")
    assert not folder_is_unavailable(None)
    assert not any(folder_is_unavailable(r) for r in WATCHER_UNAVAILABLE_REASONS)


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


@pytest.mark.parametrize(
    ("reason", "after"),
    [("root_missing", None), ("root_unreadable", None), ("root_empty", None), ("unmounted", "unmounted")],
)
async def test_disabling_a_folder_clears_only_the_watchers_reasons(client, admin_token, db_session, reason, after):
    """A note about a folder the watcher no longer watches would stick forever;
    discovery's "unmounted" is not the API's to clear."""
    folder = WatchedFolder(path="/w", display_name="w", bookmark_data=None, unavailable_reason=reason)
    db_session.add(folder)
    await db_session.flush()
    r = await client.patch(
        f"/api/watch/folders/{folder.folder_id}", json={"enabled": False}, headers=auth_header(admin_token)
    )
    assert r.status_code == 200
    await db_session.refresh(folder)
    assert (folder.enabled, folder.unavailable_reason) == (False, after)


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
# POST /api/watch/remove — the same retirement as a file deleted from disk (#604)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("doc_before", "doc_after"), [("active", "removed"), ("deleted", "deleted")])
async def test_remove_hides_the_document_but_never_overrides_an_admin_delete(
    client, admin_token, db_session, doc_before, doc_after
):
    folder = WatchedFolder(path="/w", display_name="w", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()
    doc = Document(
        title="a",
        canonical_filename="a.txt",
        status=doc_before,
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()
    wf = WatchedFile(
        folder_id=folder.folder_id, relative_path="a.txt", bookmark_data=b"", sha256=b"\x01" * 32, doc_id=doc.doc_id
    )
    db_session.add(wf)
    await db_session.flush()

    resp = await client.post(
        "/api/watch/remove",
        headers=auth_header(admin_token),
        json={"folder_id": str(folder.folder_id), "relative_path": "a.txt"},
    )
    assert resp.status_code == 200
    await db_session.refresh(wf)
    await db_session.refresh(doc)
    assert wf.status == WatchedFileStatus.removed
    assert wf.removed_at is not None
    assert doc.status == doc_after


async def _seed_watched(db_session, folder, rel, *, wf_status=WatchedFileStatus.active, doc_status="active", sha=None):
    sha = sha or bytes([len(rel)]) * 32
    doc = Document(
        title=rel.rsplit(".", 1)[0].rsplit("/", 1)[-1],
        canonical_filename=rel.rsplit("/", 1)[-1],
        status=doc_status,
        sha256=sha,
        pipeline_status=PipelineStatus.ready,
        source_path=f"{folder.path}/{rel}",
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    wf = WatchedFile(
        folder_id=folder.folder_id,
        relative_path=rel,
        bookmark_data=b"",
        sha256=sha,
        doc_id=doc.doc_id,
        status=wf_status,
    )
    db_session.add(wf)
    await db_session.flush()
    return wf, doc


@pytest.mark.parametrize(("doc_before", "doc_after"), [("removed", "active"), ("deleted", "deleted")])
async def test_ingest_of_a_returning_file_restores_only_a_hidden_document(
    client, admin_token, db_session, tmp_path, doc_before, doc_after
):
    """As the watcher's resurrection: an admin's "deleted" stays deleted when the file is re-ingested."""
    folder = WatchedFolder(path=str(tmp_path), display_name="w", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()
    wf, doc = await _seed_watched(
        db_session, folder, "a.txt", wf_status=WatchedFileStatus.removed, doc_status=doc_before, sha=b"\x07" * 32
    )

    resp = await client.post(
        "/api/watch/ingest",
        headers=auth_header(admin_token),
        json={
            "folder_id": str(folder.folder_id),
            "relative_path": "a.txt",
            "sha256": "07" * 32,
            "bookmark_data": "",
            "source_path": str(tmp_path / "a.txt"),
            "mime_type": "text/plain",
        },
    )
    assert resp.status_code == 200
    await db_session.refresh(wf)
    await db_session.refresh(doc)
    assert wf.status == WatchedFileStatus.active
    assert doc.status == doc_after


# ---------------------------------------------------------------------------
# POST /api/watch/rename — the watcher's move rules (watcher/relocate.py, #847)
# ---------------------------------------------------------------------------


def _rename_body(folder, tmp_path, old, new):
    return {
        "folder_id": str(folder.folder_id),
        "old_relative_path": old,
        "new_relative_path": new,
        "bookmark_data": "",
        "source_path": str(tmp_path / new),
    }


async def _folder(db_session, tmp_path):
    folder = WatchedFolder(path=str(tmp_path), display_name="w", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()
    return folder


async def test_rename_moves_the_paths_name_and_derived_title(client, admin_token, db_session, tmp_path):
    folder = await _folder(db_session, tmp_path)
    wf, doc = await _seed_watched(db_session, folder, "a.txt")

    resp = await client.post(
        "/api/watch/rename", headers=auth_header(admin_token), json=_rename_body(folder, tmp_path, "a.txt", "sub/b.txt")
    )
    assert resp.status_code == 200
    await db_session.refresh(wf)
    await db_session.refresh(doc)
    assert wf.relative_path == "sub/b.txt"
    assert (doc.source_path, doc.canonical_filename, doc.title) == (str(tmp_path / "sub/b.txt"), "b.txt", "b")
    assert doc.pipeline_seq == 0, "same type, no sidecar: nothing to extract again"


async def test_rename_to_another_type_reextracts(client, admin_token, db_session, tmp_path):
    folder = await _folder(db_session, tmp_path)
    wf, doc = await _seed_watched(db_session, folder, "notes.txt")

    resp = await client.post(
        "/api/watch/rename",
        headers=auth_header(admin_token),
        json=_rename_body(folder, tmp_path, "notes.txt", "notes.md"),
    )
    assert resp.status_code == 200
    await db_session.refresh(doc)
    assert doc.mime_type == "text/markdown"
    assert doc.pipeline_seq == 1
    jobs = (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == doc.doc_id))).scalars().all()
    assert [(j.stage, j.status) for j in jobs] == [(JobStage.extract, JobStatus.queued)]


@pytest.mark.parametrize("same_content", [False, True], ids=["new-content", "same-content"])
async def test_rename_onto_a_live_file_replaces_it_as_the_watcher_does(
    client, admin_token, db_session, tmp_path, same_content
):
    """`mv a.txt b.txt` over a live b.txt: b.txt keeps its document (the one its
    path cites) and takes a's content, reprocessed when it differs; a.txt's row
    is retired as a deleted file. One rule with the watcher's moved event."""
    folder = await _folder(db_session, tmp_path)
    a_wf, a_doc = await _seed_watched(db_session, folder, "a.txt", sha=b"\x0a" * 32)
    b_wf, b_doc = await _seed_watched(db_session, folder, "b.txt", sha=b"\x0a" * 32 if same_content else b"\x0b" * 32)

    resp = await client.post(
        "/api/watch/rename", headers=auth_header(admin_token), json=_rename_body(folder, tmp_path, "a.txt", "b.txt")
    )
    assert resp.status_code == 200
    assert resp.json() == {"action": "replaced", "doc_id": str(b_doc.doc_id)}
    for row in (a_wf, a_doc, b_wf, b_doc):
        await db_session.refresh(row)
    assert (a_wf.relative_path, a_wf.status, a_doc.status) == ("a.txt", WatchedFileStatus.removed, "removed")
    assert (b_wf.status, b_doc.status) == (WatchedFileStatus.active, "active")
    assert b_wf.sha256 == b_doc.sha256 == b"\x0a" * 32
    assert b_doc.pipeline_seq == (0 if same_content else 1)


async def test_rename_onto_a_retired_path_keeps_the_moved_document(client, admin_token, db_session, tmp_path):
    folder = await _folder(db_session, tmp_path)
    wf, doc = await _seed_watched(db_session, folder, "draft.txt")
    _old_wf, old_doc = await _seed_watched(
        db_session, folder, "report.txt", wf_status=WatchedFileStatus.removed, doc_status="removed"
    )
    old_doc_id, doc_id, folder_id = old_doc.doc_id, doc.doc_id, folder.folder_id

    resp = await client.post(
        "/api/watch/rename",
        headers=auth_header(admin_token),
        json=_rename_body(folder, tmp_path, "draft.txt", "report.txt"),
    )
    assert resp.status_code == 200
    db_session.expire_all()
    rows = (await db_session.execute(select(WatchedFile).where(WatchedFile.folder_id == folder_id))).scalars().all()
    assert [(r.relative_path, r.doc_id, r.status) for r in rows] == [("report.txt", doc_id, WatchedFileStatus.active)]
    assert await db_session.get(Document, old_doc_id) is None
