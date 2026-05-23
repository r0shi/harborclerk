import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.main import WatcherDaemon


@pytest.fixture
def factory(_sync_engine):
    """sessionmaker bound to the test sync engine."""
    return sessionmaker(bind=_sync_engine, expire_on_commit=False)


def _commit_folder(factory, path: str) -> uuid.UUID:
    sess = factory()
    try:
        folder = WatchedFolder(path=path, display_name=Path(path).name, bookmark_data=None)
        sess.add(folder)
        sess.commit()
        return folder.folder_id
    finally:
        sess.close()


def _truncate_watched(factory):
    """Clean up watched_folders/watched_files between tests since this test does its own commits outside the cleanup-on-yield fixture pattern."""
    from harbor_clerk.models.document import Document
    from harbor_clerk.models.ingestion_job import IngestionJob

    sess = factory()
    try:
        # Order matters: FK chain
        sess.query(WatchedFile).delete()
        sess.query(WatchedFolder).delete()
        sess.query(IngestionJob).delete()
        sess.query(Document).delete()
        sess.commit()
    finally:
        sess.close()


def test_daemon_picks_up_existing_folder_and_ingests_new_files(factory, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "")  # disable docker discovery
    _truncate_watched(factory)
    folder_id = _commit_folder(factory, str(tmp_path))

    daemon = WatcherDaemon(factory)
    daemon.start()
    try:
        # Give the observer a moment to register
        time.sleep(0.5)
        (tmp_path / "doc.pdf").write_bytes(b"hello")

        deadline = time.time() + 5.0
        wf = None
        while time.time() < deadline:
            sess = factory()
            try:
                wf = sess.query(WatchedFile).filter_by(folder_id=folder_id).one_or_none()
            finally:
                sess.close()
            if wf is not None and wf.status == WatchedFileStatus.active:
                break
            time.sleep(0.1)

        assert wf is not None, "WatchedFile row not created within 5s of file drop"
        assert wf.relative_path == "doc.pdf"
        assert wf.status == WatchedFileStatus.active
    finally:
        daemon.stop()
        _truncate_watched(factory)


def test_daemon_reacts_to_folder_added_via_notify(factory, tmp_path, monkeypatch):
    """Adding a new folder via NOTIFY should cause the daemon to register an observer for it."""
    from harbor_clerk.watcher.notify import notify_folder_change

    monkeypatch.setenv("WATCH_ROOT", "")
    _truncate_watched(factory)

    daemon = WatcherDaemon(factory)
    daemon.start()
    try:
        # No folder yet — wait briefly to ensure listener thread is up
        time.sleep(0.5)

        # Add a folder + fire NOTIFY
        folder_id = _commit_folder(factory, str(tmp_path))
        sess = factory()
        try:
            notify_folder_change(sess, folder_id, action="added")
            sess.commit()
        finally:
            sess.close()

        # Give the daemon a moment to react and register the observer
        time.sleep(0.7)

        # Drop a file; the daemon should now be watching this directory
        (tmp_path / "post-add.txt").write_bytes(b"new")

        deadline = time.time() + 5.0
        wf = None
        while time.time() < deadline:
            sess = factory()
            try:
                wf = sess.query(WatchedFile).filter_by(folder_id=folder_id).one_or_none()
            finally:
                sess.close()
            if wf is not None:
                break
            time.sleep(0.1)

        assert wf is not None, "WatchedFile not created after NOTIFY-triggered observer registration"
        assert wf.relative_path == "post-add.txt"
    finally:
        daemon.stop()
        _truncate_watched(factory)


def test_daemon_scans_existing_files_when_observer_registers(factory, tmp_path, monkeypatch):
    """Files already present in the folder when the daemon registers the
    observer must be ingested via the initial scan path (regression: prior to
    this fix, only files added AFTER observer-start were caught)."""
    monkeypatch.setenv("WATCH_ROOT", "")
    _truncate_watched(factory)

    # Create files BEFORE the daemon starts watching the folder.
    (tmp_path / "pre-existing.pdf").write_bytes(b"existing content")
    (tmp_path / "ignored.exe").write_bytes(b"binary noise")  # filtered by extension allowlist
    (tmp_path / "._stuff.pdf").write_bytes(b"AppleDouble junk")  # filtered by AppleDouble check

    folder_id = _commit_folder(factory, str(tmp_path))

    daemon = WatcherDaemon(factory)
    daemon.start()
    try:
        # Wait for the initial scan thread to finish — bounded.
        deadline = time.time() + 10.0
        rows: list[WatchedFile] = []
        while time.time() < deadline:
            sess = factory()
            try:
                rows = list(sess.query(WatchedFile).filter_by(folder_id=folder_id).all())
            finally:
                sess.close()
            if rows:
                break
            time.sleep(0.1)

        assert len(rows) == 1, f"expected exactly one ingested file, got {[r.relative_path for r in rows]}"
        assert rows[0].relative_path == "pre-existing.pdf"

        # Also verify last_scan_at flipped (scan_status idle in the API).
        sess = factory()
        try:
            folder = sess.query(WatchedFolder).filter_by(folder_id=folder_id).one()
            assert folder.last_scan_at is not None, "last_scan_at must be set after initial scan completes"
        finally:
            sess.close()
    finally:
        daemon.stop()
        _truncate_watched(factory)


def test_scan_folder_writes_skipped_count_and_extensions(factory, tmp_path, monkeypatch):
    """After _scan_folder visits a folder containing unsupported files,
    watched_folders.skipped_count and .skipped_extensions are updated.
    Dotfiles, AppleDouble, __MACOSX, and *.excalidraw.md are NOT counted.

    Covers the realistic vault layout from spec Phase 5 (Task 6 in the plan):
    PDF + Markdown accepted, two unsupported extensions tallied, dotfile +
    AppleDouble shadow + Excalidraw note classified as noise (not counted).

    NB: the plan suggested ``board.canvas`` as an unsupported file, but Phase 1
    added ``.canvas`` to the plain-text allowlist. ``blueprint.dwg`` (a CAD
    format that is not allowlisted) stands in.
    """
    from sqlalchemy import select

    from harbor_clerk.models.watched import WatchedFolder
    from harbor_clerk.watcher.main import WatcherDaemon

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")

    # Layout:
    #   doc.pdf                → accepted
    #   notes.md               → accepted
    #   malware.exe            → counted (unsupported)
    #   blueprint.dwg          → counted (unsupported)
    #   diagram.excalidraw.md  → NOT counted (noise: excalidraw)
    #   .DS_Store              → NOT counted (noise: dotfile)
    #   ._shadow.pdf           → NOT counted (noise: AppleDouble)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake pdf\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    (tmp_path / "malware.exe").write_bytes(b"MZ")
    (tmp_path / "blueprint.dwg").write_bytes(b"AC1027")
    (tmp_path / "diagram.excalidraw.md").write_text("---\nexcalidraw-plugin: parsed\n---\n")
    (tmp_path / ".DS_Store").write_bytes(b"")
    (tmp_path / "._shadow.pdf").write_bytes(b"")

    # Create the folder row first.
    sess = factory()
    folder = WatchedFolder(path=str(tmp_path), auto_discovered=False)
    sess.add(folder)
    sess.commit()
    folder_id = folder.folder_id
    sess.close()

    try:
        daemon = WatcherDaemon(factory)
        # Avoid spinning up real observers — only the scan logic is under test.
        daemon._scan_folder(folder_id, str(tmp_path))

        sess = factory()
        try:
            row = sess.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder_id)).scalar_one()
            assert row.skipped_count == 2, (
                f"expected 2 (malware.exe + blueprint.dwg), got {row.skipped_count}; "
                f"extensions={row.skipped_extensions!r}"
            )
            # Lowercased, sorted, distinct.
            assert row.skipped_extensions == [".dwg", ".exe"]
            # Scan completion marker also set.
            assert row.last_scan_at is not None
        finally:
            sess.close()
    finally:
        _truncate_watched(factory)


def test_scan_folder_records_no_extension_sentinel(factory, tmp_path, monkeypatch):
    """Extension-less files (README, LICENSE) are UNSUPPORTED but Path.suffix
    is "". They contribute to ``skipped_count`` and appear under the
    ``"(no extension)"`` sentinel in ``skipped_extensions`` so the per-folder
    UI summary reflects them instead of leaving the count unexplained."""
    from sqlalchemy import select

    from harbor_clerk.models.watched import WatchedFolder
    from harbor_clerk.watcher.main import WatcherDaemon

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")

    (tmp_path / "README").write_text("readme\n")
    (tmp_path / "LICENSE").write_text("license\n")
    (tmp_path / "malware.exe").write_bytes(b"MZ")

    sess = factory()
    folder = WatchedFolder(path=str(tmp_path), auto_discovered=False)
    sess.add(folder)
    sess.commit()
    folder_id = folder.folder_id
    sess.close()

    try:
        daemon = WatcherDaemon(factory)
        daemon._scan_folder(folder_id, str(tmp_path))

        sess = factory()
        try:
            row = sess.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder_id)).scalar_one()
            assert row.skipped_count == 3, (
                f"expected 3 (README + LICENSE + malware.exe), got {row.skipped_count}; "
                f"extensions={row.skipped_extensions!r}"
            )
            # ".exe" + "(no extension)" sentinel (covering both README and LICENSE).
            assert row.skipped_extensions == ["(no extension)", ".exe"]
        finally:
            sess.close()
    finally:
        _truncate_watched(factory)
