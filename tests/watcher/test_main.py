import threading
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
            # Wait for the condition the test actually asserts — the row *and*
            # last_scan_at — not just the row. The scan thread writes the row
            # first and stamps last_scan_at afterwards, so polling on rows alone
            # could win that race and fail the last_scan_at assertion below (#607).
            if rows:
                sess2 = factory()
                try:
                    done = sess2.query(WatchedFolder).filter_by(folder_id=folder_id).one().last_scan_at is not None
                finally:
                    sess2.close()
                if done:
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


def _watched_file_paths(factory, folder_id, status=None) -> list[str]:
    sess = factory()
    try:
        q = sess.query(WatchedFile).filter_by(folder_id=folder_id)
        if status is not None:
            q = q.filter_by(status=status)
        return sorted(wf.relative_path for wf in q.all())
    finally:
        sess.close()


def test_scan_folder_skips_sidecar_json_without_counting_it(factory, tmp_path, monkeypatch):
    """Initial scan path (#728): `<stem>.json` beside a document is neither
    dispatched nor tallied as unsupported; a .json on its own is a document."""
    from sqlalchemy import select

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")

    (tmp_path / "INV-001.pdf").write_bytes(b"%PDF-1.4\n%invoice\n")
    (tmp_path / "INV-001.json").write_text('{"vendor": "Acme"}')
    (tmp_path / "lone.json").write_text('{"kind": "export"}')
    (tmp_path / "malware.exe").write_bytes(b"MZ")

    sess = factory()
    folder = WatchedFolder(path=str(tmp_path), auto_discovered=False)
    sess.add(folder)
    sess.commit()
    folder_id = folder.folder_id
    sess.close()

    try:
        daemon = WatcherDaemon(factory)
        dispatched: list[str] = []
        real_on_event = daemon._on_event

        def _recording_on_event(event):
            dispatched.append(event.relative_path)
            real_on_event(event)

        daemon._on_event = _recording_on_event
        daemon._scan_folder(folder_id, str(tmp_path))

        # The scan classifies before dispatching so a sidecar costs no hash
        # and no round trip; handle_event would skip it, but must not be asked.
        assert sorted(dispatched) == ["INV-001.pdf", "lone.json"]
        assert _watched_file_paths(factory, folder_id) == ["INV-001.pdf", "lone.json"]
        sess = factory()
        try:
            row = sess.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder_id)).scalar_one()
            assert row.skipped_count == 1, f"only malware.exe counts, got {row.skipped_count}"
            assert row.skipped_extensions == [".exe"]
            assert row.last_scan_at is not None
        finally:
            sess.close()
    finally:
        _truncate_watched(factory)


def test_scan_folder_retires_sidecar_rows_an_earlier_build_indexed(factory, tmp_path, monkeypatch):
    """The first scan after the upgrade visits the document, and that visit
    retires the sidecar's row from before #728 without touching the file."""
    import harbor_clerk.watcher.events as ev
    from harbor_clerk.models.document import Document

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")

    (tmp_path / "INV-001.pdf").write_bytes(b"%PDF-1.4\n%invoice\n")
    sidecar = tmp_path / "INV-001.json"
    sidecar.write_text('{"vendor": "Acme"}')
    sidecar_bytes = sidecar.read_bytes()

    sess = factory()
    folder = WatchedFolder(path=str(tmp_path), auto_discovered=False)
    sess.add(folder)
    sess.commit()
    folder_id = folder.folder_id
    sess.close()

    try:
        daemon = WatcherDaemon(factory)
        # Rows as the earlier build left them: both files indexed.
        with monkeypatch.context() as m:
            m.setattr(ev, "sidecar_owner_names", lambda _path, sibling_names=None: [])
            m.setattr(ev, "_retire_sidecar_row", lambda *_args: False)
            daemon._scan_folder(folder_id, str(tmp_path))
        assert _watched_file_paths(factory, folder_id, WatchedFileStatus.active) == ["INV-001.json", "INV-001.pdf"]

        daemon._scan_folder(folder_id, str(tmp_path))

        assert _watched_file_paths(factory, folder_id, WatchedFileStatus.active) == ["INV-001.pdf"]
        assert _watched_file_paths(factory, folder_id, WatchedFileStatus.removed) == ["INV-001.json"]
        sess = factory()
        try:
            statuses = {d.canonical_filename: d.status for d in sess.query(Document).all()}
        finally:
            sess.close()
        assert statuses == {"INV-001.pdf": "active", "INV-001.json": "removed"}
        assert sidecar.read_bytes() == sidecar_bytes, "the watcher never modifies a source"
    finally:
        _truncate_watched(factory)


# --- #593: one transient database error must not end a background thread ----


def _db_blip() -> Exception:
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT watched_folders ...", None, OSError("server closed the connection unexpectedly"))


def _run_in_thread(target) -> threading.Thread:
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


def test_listener_reconnects_after_the_connection_drops(monkeypatch, caplog):
    """A dropped LISTEN connection (a Postgres restart) ended the listener
    thread for good: folder add/remove/enable/disable from the API stopped
    taking effect until the watcher restarted. It must reconnect, resync once
    LISTEN is back (NOTIFYs sent in the gap are lost), and keep listening.
    """
    import logging

    from harbor_clerk.watcher import main as watcher_main

    monkeypatch.setattr(watcher_main, "LISTENER_RETRY_BASE_SECS", 0.01)
    attempts = []
    listening_again = threading.Event()

    def _fake_listen(stop_event, poll_interval, on_listening=None):
        attempts.append(1)
        if len(attempts) <= 2:
            raise _db_blip()  # connect or LISTEN refused while Postgres restarts
        if on_listening is not None:
            on_listening()
        listening_again.set()
        yield {"folder_id": str(uuid.uuid4()), "action": "added"}
        stop_event.wait(timeout=5)

    syncs = []
    monkeypatch.setattr(watcher_main, "listen_for_folder_changes", _fake_listen)
    daemon = WatcherDaemon(lambda: None)
    monkeypatch.setattr(daemon, "_sync_observers", lambda: syncs.append(1))

    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher.main"):
        t = _run_in_thread(daemon._listener_loop)
        try:
            assert listening_again.wait(timeout=5), "listener never reconnected after the connection dropped"
            deadline = time.monotonic() + 5
            while len(syncs) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert t.is_alive(), "listener thread ended"
        finally:
            daemon._stop.set()
            t.join(timeout=5)
    assert not t.is_alive(), "listener did not stop promptly"

    assert len(syncs) == 2, "expected a resync on reconnect plus one for the NOTIFY"
    failed = [r for r in caplog.records if "listener failed" in r.getMessage()]
    assert len(failed) == 1, f"expected one log per failure streak, got {len(failed)}"
    assert any("reconnected after 2 failed attempt(s)" in r.getMessage() for r in caplog.records)


def test_listener_stops_promptly_during_its_backoff(monkeypatch):
    from harbor_clerk.watcher import main as watcher_main

    monkeypatch.setattr(watcher_main, "LISTENER_RETRY_BASE_SECS", 30.0)
    failed = threading.Event()

    def _down(stop_event, poll_interval, on_listening=None):
        failed.set()
        raise _db_blip()
        yield  # unreachable; makes this a generator, as the real one is

    monkeypatch.setattr(watcher_main, "listen_for_folder_changes", _down)
    daemon = WatcherDaemon(lambda: None)
    t = _run_in_thread(daemon._listener_loop)
    assert failed.wait(timeout=5)
    time.sleep(0.05)
    assert t.is_alive(), "listener thread ended on a database error instead of backing off"

    daemon._stop.set()
    t.join(timeout=2)  # well inside the 30s backoff
    assert not t.is_alive(), "stop during the listener backoff did not end the thread"


def test_discovery_survives_a_failed_observer_sync(monkeypatch):
    """`_sync_observers()` sat outside the discovery loop's try, so one database
    error there ended the discovery thread for good."""
    from types import SimpleNamespace

    from harbor_clerk.watcher import main as watcher_main

    monkeypatch.setattr(watcher_main, "get_settings", lambda: SimpleNamespace(watch_root="/nonexistent-root"))
    monkeypatch.setattr(watcher_main, "scan_watch_root", lambda sess, root: None)
    monkeypatch.setattr(watcher_main, "DISCOVERY_INTERVAL_SECS", 0.01)

    class _Session:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    calls = []
    second_pass = threading.Event()

    def _sync():
        calls.append(1)
        if len(calls) == 1:
            raise _db_blip()
        second_pass.set()

    daemon = WatcherDaemon(_Session)
    monkeypatch.setattr(daemon, "_sync_observers", _sync)
    t = _run_in_thread(daemon._discovery_loop)
    try:
        assert second_pass.wait(timeout=5), "discovery thread died on a database error"
    finally:
        daemon._stop.set()
        t.join(timeout=5)
    assert not t.is_alive()


class _RecordingStop(threading.Event):
    """A stop event whose waits return at once and record the delay asked for,
    so a backoff schedule can be read without sleeping through it. Sets itself
    after `limit` waits, so a loop that never stops still ends the test."""

    def __init__(self, limit: int):
        super().__init__()
        self.delays: list[float] = []
        self.limit = limit

    def wait(self, timeout=None):
        if timeout is not None:
            self.delays.append(timeout)
            if len(self.delays) >= self.limit:
                self.set()
        return self.is_set()


def _flapping_listen(errors=None):
    """Accepts LISTEN every time, then drops at the first poll()."""
    calls = []

    def _listen(stop_event, poll_interval, on_listening=None):
        calls.append(1)
        if on_listening is not None:
            on_listening()
        raise (errors(len(calls)) if errors else _db_blip())
        yield  # unreachable; makes this a generator, as the real one is

    return _listen, calls


def _run_flapping_listener(monkeypatch, *, waits: int, healthy_secs: float | None = None, errors=None):
    from harbor_clerk.watcher import main as watcher_main

    listen, calls = _flapping_listen(errors)
    monkeypatch.setattr(watcher_main, "listen_for_folder_changes", listen)
    if healthy_secs is not None:
        monkeypatch.setattr(watcher_main, "LISTENER_HEALTHY_SECS", healthy_secs)
    daemon = WatcherDaemon(lambda: None)
    daemon._stop = _RecordingStop(limit=waits)
    syncs = []
    monkeypatch.setattr(daemon, "_sync_observers", lambda: syncs.append(1))
    t = _run_in_thread(daemon._listener_loop)
    t.join(timeout=5)
    assert not t.is_alive(), "listener loop did not end"
    return daemon._stop.delays, calls, syncs


def test_flapping_listener_backs_off(monkeypatch, caplog):
    """A connection that accepts LISTEN and drops at the first poll() used to
    reset the streak on every LISTEN: base delay forever, with a traceback, a
    resync and a "reconnected" line each time. The backoff must grow."""
    import logging

    from harbor_clerk.watcher import main as watcher_main

    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher.main"):
        delays, calls, syncs = _run_flapping_listener(monkeypatch, waits=7)

    base, cap = watcher_main.LISTENER_RETRY_BASE_SECS, watcher_main.LISTENER_RETRY_MAX_SECS
    assert delays[:6] == [min(cap, base * 2**n) for n in range(6)], f"backoff did not grow: {delays}"
    assert delays[-1] == cap
    tracebacks = [r for r in caplog.records if r.exc_info and "listener" in r.getMessage()]
    assert len(tracebacks) == 1, f"one traceback per streak, got {len(tracebacks)}"
    reconnected = [r for r in caplog.records if "reconnected" in r.getMessage()]
    assert len(reconnected) == 1, f"one 'reconnected' line per streak, got {len(reconnected)}"
    assert len(syncs) == len(calls) - 1, "every reconnect still resyncs (NOTIFYs in the gap are lost)"


def test_listener_streak_resets_after_a_healthy_connection(monkeypatch, caplog):
    """The reset still happens once a connection has stayed up a healthy period:
    a drop after hours of working is a new incident, back at the base delay, and
    its reconnect is announced again (once per streak, not once per process)."""
    import logging

    from harbor_clerk.watcher import main as watcher_main

    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher.main"):
        delays, calls, _ = _run_flapping_listener(monkeypatch, waits=4, healthy_secs=0.0)

    assert delays == [watcher_main.LISTENER_RETRY_BASE_SECS] * 4, f"healthy runs did not reset the streak: {delays}"
    # Every drop is its own streak here, and every LISTEN after the first is the
    # reconnect that ends one.
    reconnected = [r for r in caplog.records if "listener reconnected" in r.getMessage()]
    assert len(calls) == 4
    assert len(reconnected) == len(calls) - 1, (
        f"expected one 'reconnected' line per healthy-separated streak, got {len(reconnected)}"
    )


def test_listener_logs_a_changed_error_and_a_heartbeat(monkeypatch, caplog):
    """Log-once must not hide a persistent cause: an error that changes within a
    streak is logged at WARNING, and so is every tenth failure of the same one."""
    import logging

    def _errors(n):
        if n <= 2:
            return OSError("connection refused")
        return RuntimeError('password authentication failed for user "lka"')

    with caplog.at_level(logging.WARNING, logger="harbor_clerk.watcher.main"):
        _run_flapping_listener(monkeypatch, waits=20, errors=_errors)

    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 4, warned  # first, changed (3rd), heartbeats (10th, 20th)
    assert "failed; reconnecting" in warned[0]
    assert "error changed" in warned[1] and "password authentication failed" in warned[1]
    assert "failure 10, same error" in warned[2]
    assert "failure 20, same error" in warned[3]


def _aliased_watched_doc(factory, tmp_path, *, stored_source: str) -> tuple[uuid.UUID, uuid.UUID, Path]:
    """A watched folder at its realpath with one file, doc.txt, already ingested: its
    WatchedFile is active with the file's sha, and the document's source_path is
    ``stored_source`` (what an older client sent). No stored original.
    Returns (folder_id, doc_id, real folder path)."""
    import hashlib

    from harbor_clerk.models.document import Document
    from harbor_clerk.models.enums import PipelineStatus

    real = tmp_path / "real"
    real.mkdir()
    (real / "doc.txt").write_text("on disk body")
    (real / "other.txt").write_text("another file")
    sha = hashlib.sha256(b"on disk body").digest()
    sess = factory()
    try:
        folder = WatchedFolder(path=str(real), display_name="real", bookmark_data=None)
        doc = Document(
            title="doc",
            canonical_filename="doc.txt",
            status="active",
            sha256=sha,
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            source_path=stored_source,
        )
        sess.add_all([folder, doc])
        sess.flush()
        sess.add(
            WatchedFile(
                folder_id=folder.folder_id,
                relative_path="doc.txt",
                bookmark_data=b"",
                sha256=sha,
                doc_id=doc.doc_id,
                status=WatchedFileStatus.active,
            )
        )
        sess.commit()
        return folder.folder_id, doc.doc_id, real
    finally:
        sess.close()


def _read(factory, doc_id: uuid.UUID) -> tuple[str | None, bytes | None]:
    """(source_path, bytes read_document_bytes returns, or None if it could not read any)."""
    from harbor_clerk.models.document import Document
    from harbor_clerk.watched_source import SourceUnavailable, read_document_bytes

    sess = factory()
    try:
        doc = sess.get(Document, doc_id)
        try:
            data, _ = read_document_bytes(sess, doc)
        except SourceUnavailable:
            data = None
        return doc.source_path, data
    finally:
        sess.close()


def test_startup_scan_canonicalizes_an_aliased_source_path(factory, tmp_path, monkeypatch):
    """A watched document whose source_path is an alias of its file's path (a symlinked
    directory) and that has no stored original is unreadable until one scan repairs it."""
    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")
    tmp_path = tmp_path.resolve()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    folder_id, doc_id, real = _aliased_watched_doc(factory, tmp_path, stored_source=str(link / "doc.txt"))
    try:
        assert _read(factory, doc_id) == (str(link / "doc.txt"), None)

        WatcherDaemon(factory)._scan_folder(folder_id, str(real))

        assert _read(factory, doc_id) == (str(real / "doc.txt"), b"on disk body")
    finally:
        _truncate_watched(factory)


@pytest.mark.parametrize("label", ["none", "relative", "other-file"])
def test_startup_scan_heals_a_source_path_that_is_a_label(factory, tmp_path, monkeypatch, label):
    """The watched file is authoritative: a source_path that is None, a relative name,
    or another existing file (a client's label from a #859-shaped new version) is
    replaced by the watched file's path on one scan visit, and the document reads."""
    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")
    tmp_path = tmp_path.resolve()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "doc.txt").write_text("a different file")
    stored = {"none": None, "relative": "doc.txt", "other-file": str(downloads / "doc.txt")}[label]
    folder_id, doc_id, real = _aliased_watched_doc(factory, tmp_path, stored_source=stored)
    try:
        assert _read(factory, doc_id) == (stored, None)

        WatcherDaemon(factory)._scan_folder(folder_id, str(real))

        assert _read(factory, doc_id) == (str(real / "doc.txt"), b"on disk body")
    finally:
        _truncate_watched(factory)


def test_a_document_in_a_folder_stored_with_a_dot_dot_path_is_readable(factory, tmp_path, monkeypatch):
    """A folder whose stored path is not normalized (as auto-discovery may store it):
    the scan ingests its file, and the document reads in place."""
    from harbor_clerk.models.document import Document

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")
    (tmp_path / "other").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "doc.txt").write_text("dot dot body")
    folder_path = f"{tmp_path}/other/../real"
    folder_id = _commit_folder(factory, folder_path)
    try:
        WatcherDaemon(factory)._scan_folder(folder_id, folder_path)

        sess = factory()
        try:
            doc_id = sess.query(Document).one().doc_id
        finally:
            sess.close()
        assert _read(factory, doc_id)[1] == b"dot dot body"
    finally:
        _truncate_watched(factory)


def _queued_extracts(factory, doc_id) -> int:
    from harbor_clerk.models.enums import JobStage, JobStatus
    from harbor_clerk.models.ingestion_job import IngestionJob

    sess = factory()
    try:
        return (
            sess.query(IngestionJob).filter_by(doc_id=doc_id, stage=JobStage.extract, status=JobStatus.queued).count()
        )
    finally:
        sess.close()


def test_startup_scan_re_extracts_a_doc_indexed_from_an_older_upload(factory, tmp_path, monkeypatch):
    """A #859-shaped document (its sha256, pages and stored original from an upload,
    its watched file holding other bytes) is reprocessed from the file on one scan
    visit, so search and download agree."""
    import hashlib

    from harbor_clerk.models.document import Document
    from harbor_clerk.models.document_page import DocumentPage
    from harbor_clerk.worker.stages.extract import run_extract

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")
    tmp_path = tmp_path.resolve()
    folder_id, doc_id, real = _aliased_watched_doc(factory, tmp_path, stored_source="doc.txt")
    sess = factory()
    try:
        doc = sess.get(Document, doc_id)
        doc.sha256 = hashlib.sha256(b"older upload").digest()
        doc.original_bucket = "originals"
        doc.original_object_key = "originals/docs/x/doc.txt"
        sess.add(DocumentPage(doc_id=doc_id, page_num=1, page_text="older upload"))
        sess.commit()
        seq = doc.pipeline_seq
    finally:
        sess.close()
    try:
        WatcherDaemon(factory)._scan_folder(folder_id, str(real))

        sess = factory()
        try:
            doc = sess.get(Document, doc_id)
            assert doc.sha256 == hashlib.sha256(b"on disk body").digest()
            assert doc.pipeline_seq == seq + 1
            assert doc.source_path == str(real / "doc.txt")
        finally:
            sess.close()
        assert _queued_extracts(factory, doc_id) == 1

        run_extract(doc_id)
        sess = factory()
        try:
            pages = sess.query(DocumentPage).filter_by(doc_id=doc_id).all()
            assert [p.page_text for p in pages] == ["on disk body"]
        finally:
            sess.close()
    finally:
        _truncate_watched(factory)


def test_startup_scan_does_not_reprocess_a_current_doc(factory, tmp_path, monkeypatch):
    """A document whose sha256 already matches its watched file is not reprocessed."""
    from harbor_clerk.models.document import Document

    _truncate_watched(factory)
    monkeypatch.setenv("WATCH_ROOT", "")
    tmp_path = tmp_path.resolve()
    folder_id, doc_id, real = _aliased_watched_doc(factory, tmp_path, stored_source="doc.txt")
    sess = factory()
    try:
        seq = sess.get(Document, doc_id).pipeline_seq
    finally:
        sess.close()
    try:
        WatcherDaemon(factory)._scan_folder(folder_id, str(real))

        sess = factory()
        try:
            doc = sess.get(Document, doc_id)
            assert doc.source_path == str(real / "doc.txt")
            assert doc.pipeline_seq == seq
        finally:
            sess.close()
        assert _queued_extracts(factory, doc_id) == 0
    finally:
        _truncate_watched(factory)


def test_discovery_logs_an_unusable_watch_root_at_startup(monkeypatch, caplog):
    """A missing WATCH_ROOT was skipped at debug level on every pass; it is an ERROR once at startup (#871)."""
    import logging
    from types import SimpleNamespace

    from harbor_clerk.watcher import main as watcher_main

    monkeypatch.setattr(watcher_main, "get_settings", lambda: SimpleNamespace(watch_root="/nonexistent-root"))
    monkeypatch.setattr(watcher_main, "scan_watch_root", lambda sess, root: None)

    class _Session:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    daemon = WatcherDaemon(_Session)
    monkeypatch.setattr(daemon, "_sync_observers", lambda: None)
    daemon._stop.set()
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.watcher.discovery"):
        daemon._discovery_loop()
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "watcher: WATCH_ROOT=/nonexistent-root does not exist" in errors[0]
