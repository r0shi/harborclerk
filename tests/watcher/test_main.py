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
    from harbor_clerk.models.document_version import DocumentVersion
    from harbor_clerk.models.ingestion_job import IngestionJob

    sess = factory()
    try:
        # Order matters: FK chain
        sess.query(WatchedFile).delete()
        sess.query(WatchedFolder).delete()
        sess.query(IngestionJob).delete()
        sess.query(DocumentVersion).delete()
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
