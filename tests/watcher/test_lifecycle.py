"""The watcher against a real folder: the daemon, its real FolderObserver and the test database.

Deletes, renames, moves, atomic saves, deletes while the app was down, and a
replaced or missing root (#604, #847, #739). Every wait is bounded, so a
regression fails here instead of hanging the suite.
"""

import hashlib
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

import harbor_clerk.watcher.main as watcher_main
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.main import WatcherDaemon
from harbor_clerk.watcher.visibility import retire_watched_file
from tests.watcher._fs import case_insensitive_only, case_sensitive_only

WAIT_SECS = 10.0
ACTIVE = WatchedFileStatus.active
REMOVED = WatchedFileStatus.removed


@pytest.fixture
def factory(_sync_engine):
    return sessionmaker(bind=_sync_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _quiet_daemon(monkeypatch, factory):
    """No Docker discovery, no periodic pass unless a test asks, a fast root check, clean tables."""
    monkeypatch.setenv("WATCH_ROOT", "")
    monkeypatch.setenv("WATCH_RESCAN_INTERVAL_SECS", "0")
    monkeypatch.setattr(watcher_main, "ROOT_CHECK_INTERVAL_SECS", 0.2)
    _truncate(factory)
    yield
    _truncate(factory)


def _truncate(factory):
    sess = factory()
    try:
        sess.query(WatchedFile).delete()
        sess.query(WatchedFolder).delete()
        sess.query(IngestionJob).delete()
        sess.query(Document).delete()
        sess.commit()
    finally:
        sess.close()


def _wait_for(what: str, probe: Callable[[], object], timeout: float = WAIT_SECS):
    """Poll ``probe`` until it returns something truthy; fail with the last value after ``timeout``."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last saw {last!r}")


def _state(factory, folder_id) -> dict[str, tuple]:
    """relative_path -> (row status, doc_id, document status, document sha, pipeline_seq)."""
    sess = factory()
    try:
        out = {}
        for wf in sess.query(WatchedFile).filter_by(folder_id=folder_id).all():
            doc = sess.get(Document, wf.doc_id) if wf.doc_id else None
            out[wf.relative_path] = (
                wf.status,
                wf.doc_id,
                doc.status if doc else None,
                doc.sha256 if doc else None,
                doc.pipeline_seq if doc else None,
            )
        return out
    finally:
        sess.close()


def _sha(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def _last_scan_at(factory, folder_id):
    sess = factory()
    try:
        return sess.query(WatchedFolder).filter_by(folder_id=folder_id).one().last_scan_at
    finally:
        sess.close()


def _add_folder(factory, root: Path) -> uuid.UUID:
    sess = factory()
    try:
        folder = WatchedFolder(path=str(root), display_name=root.name, bookmark_data=None)
        sess.add(folder)
        sess.commit()
        return folder.folder_id
    finally:
        sess.close()


class _Running:
    """A started daemon whose attach-time reconciliation has finished."""

    def __init__(self, factory, folder_id):
        self.factory = factory
        self.folder_id = folder_id
        self.daemon = WatcherDaemon(factory)

    def __enter__(self):
        before = _last_scan_at(self.factory, self.folder_id)
        self.daemon.start()
        _wait_for(
            "the attach-time reconciliation",
            lambda: (stamp := _last_scan_at(self.factory, self.folder_id)) is not None and stamp != before,
        )
        # FSEvents may deliver a stream's first events a moment after it starts.
        time.sleep(0.3)
        return self

    def __exit__(self, *exc):
        self.daemon.stop()

    def state(self):
        return _state(self.factory, self.folder_id)

    def wait_for(self, what, predicate, timeout=WAIT_SECS):
        def probe():
            st = self.state()
            return st if predicate(st) else None

        try:
            return _wait_for(what, probe, timeout)
        except AssertionError as exc:
            raise AssertionError(f"{exc}; state={self.state()!r}") from None


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "watched"
    root.mkdir()
    return root


def test_deleted_file_leaves_search_at_once(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        assert run.state()["a.txt"][0:3:2] == (ACTIVE, "active")
        (root / "a.txt").unlink()
        run.wait_for("a.txt removed and hidden", lambda st: st["a.txt"][0] == REMOVED and st["a.txt"][2] == "removed")


def test_rename_keeps_the_document(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["a.txt"][1]
        (root / "a.txt").rename(root / "b.txt")
        st = run.wait_for("the row to follow the rename", lambda st: "b.txt" in st and "a.txt" not in st)
        assert st["b.txt"] == (ACTIVE, doc_id, "active", _sha("alpha"), 0)


def test_move_into_a_subfolder_keeps_the_document(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "sub").mkdir()
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["a.txt"][1]
        (root / "a.txt").rename(root / "sub" / "a.txt")
        st = run.wait_for("the row to follow the move", lambda st: "sub/a.txt" in st and "a.txt" not in st)
        assert st["sub/a.txt"] == (ACTIVE, doc_id, "active", _sha("alpha"), 0)
    sess = factory()
    try:
        assert sess.get(Document, doc_id).source_path == str(root / "sub" / "a.txt")
    finally:
        sess.close()


def test_move_out_of_the_tree_removes_and_back_in_restores(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["a.txt"][1]
        (root / "a.txt").rename(outside / "a.txt")
        run.wait_for("a.txt removed and hidden", lambda st: st["a.txt"][0:3:2] == (REMOVED, "removed"))
        (outside / "a.txt").rename(root / "a.txt")
        st = run.wait_for("a.txt restored", lambda st: st["a.txt"][0:3:2] == (ACTIVE, "active"))
        assert st["a.txt"][1] == doc_id


def test_atomic_save_reindexes_and_never_ends_removed(factory, tmp_path):
    """An editor's save: new content written to a temp file, renamed over the original."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("version one")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["a.txt"][1]
        tmp = root / "a.txt.tmp"
        tmp.write_text("version two")
        os.replace(tmp, root / "a.txt")
        run.wait_for("the new content indexed", lambda st: st["a.txt"][3] == _sha("version two"))
        # Late events for the old inode (FSEvents reports its deletion after
        # the rename) must not retire the live file.
        time.sleep(1.0)
        assert run.state() == {"a.txt": (ACTIVE, doc_id, "active", _sha("version two"), 1)}


def test_delete_then_recreate_ends_active_and_reindexed(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("version one")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        (root / "a.txt").unlink()
        (root / "a.txt").write_text("version two")
        run.wait_for("the new content indexed", lambda st: st["a.txt"][3] == _sha("version two"))
        time.sleep(1.0)
        assert run.state()["a.txt"][0:3:2] == (ACTIVE, "active")


def test_changes_while_down_are_reconciled_at_startup(factory, tmp_path):
    root = _root(tmp_path)
    for name, text in (("gone.txt", "g"), ("kept.txt", "k"), ("edited.txt", "v1")):
        (root / name).write_text(text)
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        assert sorted(run.state()) == ["edited.txt", "gone.txt", "kept.txt"]

    (root / "gone.txt").unlink()
    (root / "edited.txt").write_text("v2")
    (root / "new.txt").write_text("n")
    # Let the changes age past FSEvents' delivery window: a stream started a
    # moment after them can still report them, which would let the observer,
    # not the startup reconciliation, do the work this test is about.
    time.sleep(2.0)

    with _Running(factory, folder_id) as run:
        st = run.state()
    assert st["gone.txt"][0:3:2] == (REMOVED, "removed")
    assert st["kept.txt"][0:3:2] == (ACTIVE, "active")
    assert (st["edited.txt"][0], st["edited.txt"][3], st["edited.txt"][4]) == (ACTIVE, _sha("v2"), 1)
    assert st["new.txt"][0:3:2] == (ACTIVE, "active")


def test_periodic_rescan_removes_what_an_event_missed(factory, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_RESCAN_INTERVAL_SECS", "0.5")
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        # A row for a file that is not there, as a dropped delete would leave it.
        sess = factory()
        try:
            doc = Document(
                title="ghost",
                canonical_filename="ghost.txt",
                status="active",
                sha256=_sha("ghost"),
                pipeline_status=PipelineStatus.ready,
            )
            sess.add(doc)
            sess.flush()
            sess.add(
                WatchedFile(
                    folder_id=folder_id,
                    relative_path="ghost.txt",
                    bookmark_data=b"",
                    sha256=_sha("ghost"),
                    doc_id=doc.doc_id,
                )
            )
            sess.commit()
        finally:
            sess.close()
        run.wait_for("the periodic pass to retire ghost.txt", lambda st: st["ghost.txt"][0:3:2] == (REMOVED, "removed"))
        assert run.state()["a.txt"][0:3:2] == (ACTIVE, "active")


def test_replaced_root_is_observed_again_and_reconciled(factory, tmp_path):
    """#739: the folder is moved aside and a new one with the same name put in its place."""
    root = _root(tmp_path)
    (root / "same-name.txt").write_text("old content")
    (root / "old-only.txt").write_text("old")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["same-name.txt"][1]
        root.rename(tmp_path / "watched-old")
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "same-name.txt").write_text("new content")
        (staged / "new-only.txt").write_text("new")
        staged.rename(root)

        st = run.wait_for(
            "the new directory reconciled",
            lambda st: (
                st.get("same-name.txt", (None,) * 4)[3] == _sha("new content")
                and st.get("old-only.txt", (None,))[0] == REMOVED
                and "new-only.txt" in st
            ),
        )
        assert st["same-name.txt"][:3] == (ACTIVE, doc_id, "active")
        assert st["old-only.txt"][2] == "removed"

        # And the observer is live on the new directory, not only reconciled once.
        (root / "later.txt").write_text("later")
        run.wait_for("a file written into the new directory", lambda st: "later.txt" in st)


def test_missing_root_removes_nothing_and_is_reattached_when_back(factory, tmp_path):
    """An unmounted volume, or a folder moved aside for a while: its documents stay."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        aside = tmp_path / "aside"
        root.rename(aside)
        # Several root checks (0.2 s each) with the root gone.
        time.sleep(1.5)
        assert run.state()["a.txt"][0:3:2] == (ACTIVE, "active")
        aside.rename(root)
        (root / "b.txt").write_text("beta")
        st = run.wait_for("a file written after the root came back", lambda st: "b.txt" in st)
        assert st["a.txt"][0:3:2] == (ACTIVE, "active")


def _deafen(run: _Running) -> None:
    """Drop every event the live observer delivers, as a lost FSEvents batch would."""
    for obs in run.daemon._observers.values():
        obs._handler.sink = lambda _event: None


def test_periodic_rescan_indexes_a_modify_an_event_missed(factory, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_RESCAN_INTERVAL_SECS", "0.5")
    root = _root(tmp_path)
    (root / "a.txt").write_text("version one")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["a.txt"][1]
        _deafen(run)
        (root / "a.txt").write_text("version two")
        st = run.wait_for(
            "the periodic pass to index the new content", lambda st: st["a.txt"][3] == _sha("version two")
        )
        assert st["a.txt"] == (ACTIVE, doc_id, "active", _sha("version two"), 1)


@case_insensitive_only
def test_case_only_directory_rename_keeps_each_document(factory, tmp_path):
    root = _root(tmp_path)
    (root / "Invoices").mkdir()
    (root / "Invoices" / "x.txt").write_text("x")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["Invoices/x.txt"][1]
        (root / "Invoices").rename(root / "invoices")
        st = run.wait_for(
            "the row to follow the case-only rename",
            lambda st: "invoices/x.txt" in st and "Invoices/x.txt" not in st,
        )
        time.sleep(1.0)
        assert run.state() == {"invoices/x.txt": (ACTIVE, doc_id, "active", _sha("x"), 0)}, st


@case_insensitive_only
def test_case_only_directory_rename_while_down_keeps_each_document(factory, tmp_path):
    """After `mv Invoices invoices` with the app down, `isfile("Invoices/x.txt")`
    still answers yes on APFS: the startup pass must neither duplicate the file
    nor lose its document."""
    root = _root(tmp_path)
    (root / "Invoices").mkdir()
    (root / "Invoices" / "x.txt").write_text("x")
    (root / "Invoices" / "y.txt").write_text("y")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        before = run.state()

    (root / "Invoices").rename(root / "invoices")
    time.sleep(2.0)  # past FSEvents' delivery window, as in the test above

    with _Running(factory, folder_id) as run:
        st = run.state()
    assert st == {
        "invoices/x.txt": (ACTIVE, before["Invoices/x.txt"][1], "active", _sha("x"), 0),
        "invoices/y.txt": (ACTIVE, before["Invoices/y.txt"][1], "active", _sha("y"), 0),
    }


# ---------------------------------------------------------------------------
# What a reconciliation refuses to remove
# ---------------------------------------------------------------------------


def _reconcile_once(factory, root: Path) -> uuid.UUID:
    folder_id = _add_folder(factory, root)
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    return folder_id


def test_reconcile_of_an_empty_root_removes_nothing(factory, tmp_path):
    """An empty mount point looks exactly like a folder whose files all went."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    (root / "a.txt").unlink()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][0:3:2] == (ACTIVE, "active")
    assert _folder_row(factory, folder_id)[1] == watcher_main.ROOT_EMPTY


def test_a_root_holding_only_noise_counts_as_empty(factory, tmp_path):
    """Finder writes a .DS_Store into any folder it shows, a custom icon is an
    `Icon\\r` file, and AppleDouble or unsupported files are not documents:
    none of them makes an emptied mount point look populated."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "b.pdf").write_bytes(b"%PDF-1.4 beta")
    folder_id = _reconcile_once(factory, root)
    (root / "a.txt").unlink()
    (root / "b.pdf").unlink()
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (root / "Icon\r").write_bytes(b"")
    (root / "._b.pdf").write_bytes(b"AppleDouble")
    (root / "download.tmp").write_bytes(b"partial")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert {rel: v[0:3:2] for rel, v in st.items()} == {"a.txt": (ACTIVE, "active"), "b.pdf": (ACTIVE, "active")}
    assert _folder_row(factory, folder_id)[1] == watcher_main.ROOT_EMPTY

    (root / "c.txt").write_text("a document is back")
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert (st["a.txt"][0], st["c.txt"][0]) == (REMOVED, ACTIVE), "with a document present, removal resumes"
    assert _folder_row(factory, folder_id)[1] is None


def test_reconcile_of_a_missing_root_removes_nothing(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    root.rename(tmp_path / "aside")
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][0:3:2] == (ACTIVE, "active")


def test_reconcile_keeps_files_under_an_unreadable_directory_and_removes_the_rest(factory, tmp_path):
    """One unlistable subdirectory (permissions, TCC) must not stop removal everywhere else."""
    root = _root(tmp_path)
    (root / "top.txt").write_text("top")
    (root / "gone.txt").write_text("gone")
    sub = root / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("alpha")
    (root / "subway.txt").write_text("a sibling whose name starts like the directory's")
    folder_id = _reconcile_once(factory, root)
    (root / "gone.txt").unlink()
    (root / "subway.txt").unlink()
    sub.chmod(0)
    try:
        if os.access(sub, os.R_OK):
            pytest.skip("running with privileges that read a mode-0 directory")
        WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    finally:
        sub.chmod(0o755)
    st = _state(factory, folder_id)
    assert st["sub/a.txt"][0:3:2] == (ACTIVE, "active")
    assert st["top.txt"][0:3:2] == (ACTIVE, "active")
    assert st["gone.txt"][0:3:2] == (REMOVED, "removed")
    assert st["subway.txt"][0:3:2] == (REMOVED, "removed")


def test_reconcile_of_an_unreadable_root_removes_nothing(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    root.chmod(0o300)  # searchable, not listable
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running with privileges that read a write-only directory")
        WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    finally:
        root.chmod(0o755)
    assert _state(factory, folder_id)["a.txt"][0:3:2] == (ACTIVE, "active")


def test_reconcile_handles_an_unchanged_file_whose_row_was_retired(factory, tmp_path):
    """The stat cache skips only files whose row is active: one retired since
    the last pass (a lost event, `/watch/remove`) is compared again, and its
    file on disk restores it, as a fresh process's pass would."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    sess = factory()
    try:
        wf = sess.query(WatchedFile).filter_by(folder_id=folder_id).one()
        retire_watched_file(wf, sess.get(Document, wf.doc_id))
        sess.commit()
    finally:
        sess.close()

    daemon._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][0:3:2] == (ACTIVE, "active")


def test_events_for_one_folder_are_handled_one_at_a_time(factory, tmp_path, monkeypatch):
    """The observer thread and a reconciliation both dispatch; two creates of
    one new file at once would race into the same (folder_id, relative_path)."""
    import threading

    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    in_flight, peak, guard = [0], [0], threading.Lock()
    real_handle_event = watcher_main.handle_event

    def slow_handle_event(session, event):
        with guard:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        try:
            time.sleep(0.3)
            real_handle_event(session, event)
        finally:
            with guard:
                in_flight[0] -= 1

    monkeypatch.setattr(watcher_main, "handle_event", slow_handle_event)
    daemon = WatcherDaemon(factory)
    event = watcher_main.FileEvent(watcher_main.EventKind.created, folder_id, "a.txt", str(root / "a.txt"))
    results = []
    threads = [threading.Thread(target=lambda: results.append(daemon._on_event(event))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=WAIT_SECS)
    assert not any(t.is_alive() for t in threads), "an event handler did not return"
    assert peak[0] == 1, "two events of one folder were handled at once"
    assert results == [True, True]
    assert list(_state(factory, folder_id)) == ["a.txt"]


def test_reconcile_removes_missing_files_beside_present_ones(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    folder_id = _reconcile_once(factory, root)
    (root / "b.txt").unlink()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["a.txt"][0:3:2] == (ACTIVE, "active")
    assert st["b.txt"][0:3:2] == (REMOVED, "removed")


# ---------------------------------------------------------------------------
# A skipped pass still ends "scanning" and says why
# ---------------------------------------------------------------------------


def _folder_row(factory, folder_id) -> tuple:
    sess = factory()
    try:
        row = sess.query(WatchedFolder).filter_by(folder_id=folder_id).one()
        return row.last_scan_at, row.unavailable_reason
    finally:
        sess.close()


def test_unreadable_root_is_stamped_and_reported_until_it_is_readable(factory, tmp_path):
    """scan_status reads `last_scan_at is None` as scanning: a pass that could
    not list the root must still stamp it, and say why, or the folder shows
    "scanning" for good."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    root.chmod(0o300)  # searchable, not listable
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running with privileges that read a write-only directory")
        WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
        skipped_at, reason = _folder_row(factory, folder_id)
        assert skipped_at is not None
        assert reason == watcher_main.ROOT_UNREADABLE
    finally:
        root.chmod(0o755)

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    scanned_at, reason = _folder_row(factory, folder_id)
    assert reason is None
    assert scanned_at > skipped_at
    assert list(_state(factory, folder_id)) == ["a.txt"]


def test_missing_root_is_stamped_and_reported_until_it_returns(factory, tmp_path):
    root = tmp_path / "not-yet"
    folder_id = _add_folder(factory, root)
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    skipped_at, reason = _folder_row(factory, folder_id)
    assert skipped_at is not None
    assert reason == watcher_main.ROOT_MISSING

    root.mkdir()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    scanned_at, reason = _folder_row(factory, folder_id)
    assert (reason, scanned_at > skipped_at) == (None, True)


def test_a_root_that_goes_missing_is_reported_by_the_running_daemon(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        root.rename(tmp_path / "aside")
        _wait_for(
            "the folder to report its root missing",
            lambda: _folder_row(factory, folder_id)[1] == watcher_main.ROOT_MISSING,
        )
        assert run.state()["a.txt"][0:3:2] == (ACTIVE, "active")
        (tmp_path / "aside").rename(root)
        _wait_for("the reason to clear once the root is back", lambda: _folder_row(factory, folder_id)[1] is None)


@pytest.mark.parametrize("root_present", [True, False], ids=["completed-pass", "missing-root-pass"])
def test_a_watcher_pass_never_overwrites_dockers_unmounted(factory, tmp_path, root_present):
    """ "unmounted" belongs to Docker discovery, which re-enables a folder only
    while it reads "unmounted": a watcher pass, completed or skipped, leaves it."""
    root = _root(tmp_path) if root_present else tmp_path / "gone"
    folder_id = _add_folder(factory, root)
    sess = factory()
    try:
        sess.query(WatchedFolder).filter_by(folder_id=folder_id).one().unavailable_reason = "unmounted"
        sess.commit()
    finally:
        sess.close()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _folder_row(factory, folder_id)[1] == "unmounted"


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_root_that_is_back_is_compared_at_once(factory, tmp_path, how):
    """inotify keeps watching a root moved aside and back (same inode), so the
    observer reports `ok` throughout; the reason must still clear without
    waiting for a periodic pass (or forever, with the interval at 0)."""
    from harbor_clerk.watcher.observer import RootState

    root = tmp_path / "watched"
    if how == "unreadable":
        root.mkdir()
        root.chmod(0o300)
        if os.access(root, os.R_OK):
            root.chmod(0o755)
            pytest.skip("running with privileges that read a write-only directory")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    try:
        daemon._reconcile_folder(folder_id, str(root))
    finally:
        if how == "unreadable":
            root.chmod(0o755)
    expected = watcher_main.ROOT_MISSING if how == "missing" else watcher_main.ROOT_UNREADABLE
    assert _folder_row(factory, folder_id)[1] == expected
    root.mkdir(exist_ok=True)

    class SteadyObserver:
        def __init__(self, path):
            self.root = path

        def health(self):
            return RootState.ok

        def restart(self):
            raise AssertionError("an ok observer is not restarted")

    daemon._observers[folder_id] = SteadyObserver(str(root))
    requested = []
    daemon._request_reconcile = requested.append
    daemon._check_roots()
    assert requested == [folder_id]

    daemon._reconcile_folder(folder_id, str(root))
    assert _folder_row(factory, folder_id)[1] is None
    requested.clear()
    daemon._check_roots()
    assert requested == [], "once the reason is cleared, an ok root is left alone"


# ---------------------------------------------------------------------------
# Retries, coalescing and re-attach failures
# ---------------------------------------------------------------------------


def test_a_failed_dispatch_is_retried_on_the_next_pass(factory, tmp_path, monkeypatch):
    """The stat cache records a file only once its event was handled: a pass
    whose handling failed must not mark the new content as compared."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("version one")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    (root / "a.txt").write_text("version two")

    real_handle_event = watcher_main.handle_event

    def failing_handle_event(session, event):
        raise RuntimeError("database went away")

    monkeypatch.setattr(watcher_main, "handle_event", failing_handle_event)
    daemon._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][3] == _sha("version one")

    monkeypatch.setattr(watcher_main, "handle_event", real_handle_event)
    daemon._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][3] == _sha("version two")


def test_reconcile_requests_during_a_pass_coalesce_into_one_more_pass(factory, tmp_path):
    import threading
    from types import SimpleNamespace

    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)
    daemon._observers[folder_id] = SimpleNamespace(root=str(tmp_path))
    release, first_entered = threading.Event(), threading.Event()
    calls = []

    def fake_reconcile(fid, root):
        calls.append(fid)
        if len(calls) == 1:
            first_entered.set()
            assert release.wait(WAIT_SECS), "the test never released the first pass"

    daemon._reconcile_folder = fake_reconcile
    daemon._request_reconcile(folder_id)
    assert first_entered.wait(WAIT_SECS), "the first pass never started"
    daemon._request_reconcile(folder_id)
    daemon._request_reconcile(folder_id)
    release.set()
    _wait_for("the worker to finish", lambda: folder_id not in daemon._reconciling)
    assert calls == [folder_id, folder_id], "two requests during a pass make exactly one more pass"


def test_a_failed_reattach_is_retried_and_reconciles_only_once_attached(factory, tmp_path):
    from harbor_clerk.watcher.observer import RootState

    class FlakyObserver:
        root = str(tmp_path)

        def __init__(self):
            self.restarts = 0

        def health(self):
            return RootState.ok if self.restarts >= 2 else RootState.detached

        def restart(self):
            self.restarts += 1
            if self.restarts == 1:
                raise OSError("fsevents refused the stream")

    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)
    obs = FlakyObserver()
    daemon._observers[folder_id] = obs
    daemon._seen_stats[folder_id] = {"a.txt": (1, 2, 3, 4, 5)}
    requested = []
    daemon._request_reconcile = requested.append

    daemon._check_roots()
    assert (obs.restarts, requested) == (1, [folder_id]), "a failed attach still gets a pass, to record why"
    assert folder_id in daemon._seen_stats, "the stat cache is kept until an attach succeeds"

    daemon._check_roots()
    assert (obs.restarts, requested) == (2, [folder_id]), "a retry in a streak waits to be seen alive"
    assert folder_id in daemon._seen_stats

    daemon._check_roots()
    assert (obs.restarts, requested) == (2, [folder_id, folder_id]), "seen alive: one full pass on recovery"
    assert folder_id not in daemon._seen_stats, "a new attachment compares every file again"
    assert folder_id not in daemon._reattach_failures

    daemon._check_roots()
    assert (obs.restarts, requested) == (2, [folder_id, folder_id]), "a healthy observer is left alone"


def test_an_observer_that_cannot_attach_still_records_a_pass_and_backs_off(factory, tmp_path, monkeypatch, caplog):
    """On Linux, inotify and then polling can both refuse an unlistable root at
    start(). The folder must still be stamped and say why, and the retries must
    back off and log one traceback, not two every 5 s."""
    import logging

    from harbor_clerk.watcher.observer import FolderObserver

    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    real_start = FolderObserver.start
    attempts = []

    def refusing_start(self):
        attempts.append(self.root)
        raise PermissionError(13, "Permission denied", self.root)

    monkeypatch.setattr(FolderObserver, "start", refusing_start)
    root.chmod(0o300)  # searchable, not listable
    daemon = WatcherDaemon(factory)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running with privileges that read a write-only directory")
        daemon._sync_observers()
        _wait_for(
            "the pass to stamp the folder and record why",
            lambda: (row := _folder_row(factory, folder_id))[0] is not None and row[1] == watcher_main.ROOT_UNREADABLE,
        )
        assert attempts == [str(root)]

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
            for _ in range(10):
                daemon._check_roots()
        # Retries after 0, 1, 3 skipped checks: the 1st, 3rd and 7th of ten.
        assert len(attempts) == 4
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], "the start failure was logged once; retries are not"

        root.chmod(0o755)
        monkeypatch.setattr(FolderObserver, "start", real_start)
        for _ in range(16):  # the longest wait the backoff has reached is 7 checks
            daemon._check_roots()
            if folder_id not in daemon._reattach_failures:
                break
        assert folder_id not in daemon._reattach_failures, "the attach succeeded once the root was readable"
        _wait_for("the reason to clear", lambda: _folder_row(factory, folder_id)[1] is None)
    finally:
        root.chmod(0o755)
        daemon.stop()


# ---------------------------------------------------------------------------
# Fifth review of #860
# ---------------------------------------------------------------------------


@case_insensitive_only
def test_case_only_rename_with_a_sidecar_live(factory, tmp_path):
    root = _root(tmp_path)
    (root / "Report.txt").write_text("report body")
    (root / "Report.json").write_text('{"vendor": "Acme"}')
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["Report.txt"][1]
        assert list(run.state()) == ["Report.txt"], "the sidecar is not a document"
        (root / "Report.txt").rename(root / "report.txt")
        run.wait_for("the row to follow the rename", lambda st: "report.txt" in st and "Report.txt" not in st)
        time.sleep(1.0)
        st = run.state()
    assert list(st) == ["report.txt"], f"the sidecar stayed a sidecar: {st}"
    assert st["report.txt"][:3] == (ACTIVE, doc_id, "active")


@case_insensitive_only
def test_case_only_rename_with_a_sidecar_while_down(factory, tmp_path):
    root = _root(tmp_path)
    (root / "Report.txt").write_text("report body")
    (root / "Report.json").write_text('{"vendor": "Acme"}')
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        doc_id = run.state()["Report.txt"][1]
    (root / "Report.txt").rename(root / "report.txt")
    time.sleep(2.0)  # past FSEvents' delivery window
    with _Running(factory, folder_id) as run:
        st = run.state()
    assert list(st) == ["report.txt"], f"the sidecar stayed a sidecar: {st}"
    assert st["report.txt"][:3] == (ACTIVE, doc_id, "active")


def test_an_unreadable_file_logs_one_traceback_per_failure_streak(factory, tmp_path, caplog):
    """288 passes a day at the default interval: the traceback once, then quiet,
    and a recovery ends the streak so the next failure is loud again."""
    import logging

    root = _root(tmp_path)
    locked = root / "locked.txt"
    locked.write_text("secret")
    locked.chmod(0)
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that read a mode-0 file")
        with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
            for _ in range(5):
                daemon._reconcile_folder(folder_id, str(root))
        tracebacks = [r for r in caplog.records if r.exc_info and "locked.txt" in r.getMessage()]
        assert len(tracebacks) == 1, [r.getMessage() for r in tracebacks]

        locked.chmod(0o644)
        daemon._reconcile_folder(folder_id, str(root))
        assert _state(factory, folder_id)["locked.txt"][0:3:2] == (ACTIVE, "active")
        assert not daemon._event_failures, "a success ends the streak"

        locked.write_text("secret, edited")
        locked.chmod(0)
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
            daemon._reconcile_folder(folder_id, str(root))
        assert len([r for r in caplog.records if r.exc_info]) == 1, "a new streak is loud again"
    finally:
        locked.chmod(0o644)


def test_a_respelled_path_is_never_taken_from_under_an_unlisted_directory(factory, tmp_path, monkeypatch):
    """A file under a directory that could not be listed may still be there:
    it is not the source of a re-spelling, so its document is not handed on."""
    root = _root(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["sub/a.txt"][1]
    (root / "sub").rename(root / "SUB")
    monkeypatch.setattr(watcher_main, "_unlisted_dirs", lambda _root, _errors: ["sub"])

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["sub/a.txt"][:3] == (ACTIVE, doc_id, "active"), "protected, not moved"
    assert st["SUB/a.txt"][0] == ACTIVE and st["SUB/a.txt"][1] != doc_id


def test_a_respelled_path_inherits_only_the_same_file_or_the_same_content(factory, tmp_path, monkeypatch):
    """On a case-sensitive volume `X.txt` beside a vanished `x.txt` is another
    file: it inherits the document only if its content is the same. Simulated
    here by making every samefile answer no, as ext4 would."""
    root = _root(tmp_path)
    (root / "x.txt").write_text("the old file")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["x.txt"][1]
    (root / "x.txt").unlink()
    (root / "X.txt").write_text("a different file")
    monkeypatch.setattr(watcher_main.os.path, "samefile", lambda _a, _b: False)

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["x.txt"][0:3:2] == (REMOVED, "removed")
    assert st["X.txt"][0] == ACTIVE and st["X.txt"][1] != doc_id


def test_a_respelled_path_with_the_same_content_inherits_on_a_case_sensitive_volume(factory, tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "x.txt").write_text("the same file")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["x.txt"][1]
    (root / "x.txt").rename(root / "X.txt")
    monkeypatch.setattr(watcher_main.os.path, "samefile", lambda _a, _b: False)

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id) == {"X.txt": (ACTIVE, doc_id, "active", _sha("the same file"), 0)}


@case_sensitive_only
def test_a_case_variant_file_added_while_down_is_its_own_document(factory, tmp_path):
    root = _root(tmp_path)
    (root / "x.txt").write_text("the old file")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["x.txt"][1]
    (root / "x.txt").unlink()
    (root / "X.txt").write_text("a different file")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["x.txt"][0:3:2] == (REMOVED, "removed")
    assert st["X.txt"][0] == ACTIVE and st["X.txt"][1] != doc_id


def test_a_disabled_folder_gets_no_watcher_reason(factory, tmp_path):
    root = tmp_path / "gone"
    folder_id = _add_folder(factory, root)
    sess = factory()
    try:
        sess.query(WatchedFolder).filter_by(folder_id=folder_id).one().enabled = False
        sess.commit()
    finally:
        sess.close()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    stamped, reason = _folder_row(factory, folder_id)
    assert (stamped is not None, reason) == (True, None)


def test_a_live_event_that_indexes_a_file_clears_root_empty(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    (root / "a.txt").unlink()
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    assert _folder_row(factory, folder_id)[1] == watcher_main.ROOT_EMPTY

    (root / ".DS_Store").write_bytes(b"noise")
    daemon._on_event(
        watcher_main.FileEvent(watcher_main.EventKind.created, folder_id, ".DS_Store", str(root / ".DS_Store"))
    )
    assert _folder_row(factory, folder_id)[1] == watcher_main.ROOT_EMPTY, "noise indexes nothing"

    (root / "b.txt").write_text("beta")
    daemon._on_event(watcher_main.FileEvent(watcher_main.EventKind.created, folder_id, "b.txt", str(root / "b.txt")))
    assert _folder_row(factory, folder_id)[1] is None


def test_record_pass_never_overwrites_an_unmounted_written_while_it_runs(factory, _sync_engine, tmp_path):
    """Discovery writes "unmounted" from another connection while a pass is
    recording root_missing. A read-then-write lets the pass overwrite it; one
    conditional UPDATE does not (discovery's write waits for the row lock and
    lands after it)."""
    import threading

    from sqlalchemy import event as sa_event
    from sqlalchemy import text

    root = tmp_path / "gone"
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    recording = threading.Event()
    real_record_pass = daemon._record_pass

    def recording_pass(*args, **kwargs):
        recording.set()
        try:
            return real_record_pass(*args, **kwargs)
        finally:
            recording.clear()

    daemon._record_pass = recording_pass
    writer: list[threading.Thread] = []

    def discovery_writes_unmounted():
        with _sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE watched_folders SET unavailable_reason = 'unmounted' WHERE folder_id = :fid"),
                {"fid": folder_id},
            )

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        if recording.is_set() and not writer and "watched_folders" in statement:
            t = threading.Thread(target=discovery_writes_unmounted, daemon=True)
            writer.append(t)
            t.start()
            # A single UPDATE holds the row lock, so the writer blocks until our
            # commit; after a plain read it commits at once.
            t.join(timeout=1.0)

    sa_event.listen(_sync_engine, "after_cursor_execute", after_execute)
    try:
        daemon._reconcile_folder(folder_id, str(root))
    finally:
        sa_event.remove(_sync_engine, "after_cursor_execute", after_execute)
    assert writer, "the interleaving hook fired"
    writer[0].join(timeout=WAIT_SECS)
    assert not writer[0].is_alive(), "discovery's write never finished"
    assert _folder_row(factory, folder_id)[1] == "unmounted"


def test_a_metadata_only_change_is_not_hashed_again(factory, tmp_path, monkeypatch):
    """Evicting an iCloud file to a dataless placeholder, or downloading it
    back, changes its ctime and nothing else; hashing it again on every pass
    would download it again. A chmod is the same kind of change."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    os.chmod(root / "a.txt", 0o600)
    dispatched = []
    real_on_event = daemon._on_event
    daemon._on_event = lambda event, **kw: dispatched.append(event.relative_path) or real_on_event(event, **kw)

    daemon._reconcile_folder(folder_id, str(root))
    assert dispatched == []


# ---------------------------------------------------------------------------
# Sixth review of #860: what used to run once now runs every pass
# ---------------------------------------------------------------------------


def test_a_pass_folds_each_name_a_bounded_number_of_times(factory, tmp_path, monkeypatch):
    """The sidecar check builds one index per directory: folds grow with the
    number of files, not with its square (4,000 pairs used to cost 32 million)."""
    import harbor_clerk.watcher.events as ev

    root = _root(tmp_path)
    pairs = 150
    for i in range(pairs):
        (root / f"INV-{i:04d}.pdf").write_bytes(b"%PDF-1.4 " + str(i).encode())
        (root / f"INV-{i:04d}.json").write_text('{"vendor": "Acme"}')
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))

    folds = []
    real_fold = ev._fold
    monkeypatch.setattr(ev, "_fold", lambda name: folds.append(name) or real_fold(name))
    daemon._reconcile_folder(folder_id, str(root))
    files = 2 * pairs
    assert len(folds) <= 2 * files, f"{len(folds)} folds for {files} files"
    assert len(_state(factory, folder_id)) == pairs, "the sidecars are still sidecars"


def _outage() -> Exception:
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


def test_a_database_outage_mid_pass_ends_it_with_one_log_line(factory, tmp_path, monkeypatch, caplog):
    import logging

    root = _root(tmp_path)
    for i in range(40):
        (root / f"f{i:02d}.txt").write_text(f"file {i}")
    folder_id = _add_folder(factory, root)
    calls = []
    real_handle_event = watcher_main.handle_event

    def failing_after_three(session, event):
        calls.append(event.relative_path)
        if len(calls) > 3:
            raise _outage()
        return real_handle_event(session, event)

    monkeypatch.setattr(watcher_main, "handle_event", failing_after_three)
    daemon = WatcherDaemon(factory)
    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
        daemon._reconcile_folder(folder_id, str(root))

    assert len(calls) == 4, "the pass stopped at the first unreachable-database error"
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "unreachable" in loud[0].getMessage(), [r.getMessage() for r in loud]
    assert not daemon._event_failures, "an outage is not a per-file failure"
    assert _last_scan_at(factory, folder_id) is None, "an aborted pass compared nothing"

    monkeypatch.setattr(watcher_main, "handle_event", real_handle_event)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher"):
        daemon._reconcile_folder(folder_id, str(root))
    assert len(_state(factory, folder_id)) == 40
    assert _last_scan_at(factory, folder_id) is not None
    assert any("reachable again" in r.getMessage() for r in caplog.records)


def test_a_database_outage_at_pass_start_is_logged_and_retried(factory, tmp_path, monkeypatch, caplog):
    import logging

    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    real_active_paths = daemon._active_paths

    def unreachable(_folder_id):
        raise _outage()

    daemon._active_paths = unreachable
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.watcher"):
        daemon._reconcile_folder(folder_id, str(root))  # must not raise into the worker thread
        daemon._reconcile_folder(folder_id, str(root))
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "unreachable" in loud[0].getMessage()

    daemon._active_paths = real_active_paths
    daemon._reconcile_folder(folder_id, str(root))
    assert _last_scan_at(factory, folder_id) is not None
    assert list(_state(factory, folder_id)) == ["a.txt"]


def test_failure_streaks_are_pruned_for_vanished_paths_and_removed_folders(factory, tmp_path):
    root = _root(tmp_path)
    locked = root / "locked.txt"
    locked.write_text("secret")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that read a mode-0 file")
        daemon._reconcile_folder(folder_id, str(root))
        assert list(daemon._event_failures) == [(folder_id, "locked.txt")]
    finally:
        locked.chmod(0o644)
    locked.unlink()
    (root / "other.txt").write_text("keeps the root from looking empty")
    daemon._reconcile_folder(folder_id, str(root))
    assert daemon._event_failures == {}, "a streak for a path the pass no longer found is dropped"

    daemon._event_failures[(folder_id, "x.txt")] = watcher_main.FailureStreak()
    other = uuid.uuid4()
    daemon._event_failures[(other, "y.txt")] = watcher_main.FailureStreak()

    class Stub:
        def stop(self):
            pass

    daemon._observers[folder_id] = Stub()
    sess = factory()
    try:
        sess.query(WatchedFolder).filter_by(folder_id=folder_id).one().enabled = False
        sess.commit()
    finally:
        sess.close()
    daemon._sync_observers()
    assert list(daemon._event_failures) == [(other, "y.txt")], "a removed folder's streaks go with it"


def test_deleting_a_folder_mid_pass_ends_the_pass_quietly(factory, tmp_path, monkeypatch, caplog):
    import logging

    root = _root(tmp_path)
    for i in range(20):
        (root / f"f{i:02d}.txt").write_text(f"file {i}")
    folder_id = _add_folder(factory, root)
    calls = []
    real_handle_event = watcher_main.handle_event

    def deleting_the_folder(session, event):
        calls.append(event.relative_path)
        if len(calls) == 2:
            other = factory()
            try:
                other.query(WatchedFolder).filter_by(folder_id=folder_id).delete()
                other.commit()
            finally:
                other.close()
        return real_handle_event(session, event)

    monkeypatch.setattr(watcher_main, "handle_event", deleting_the_folder)
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.watcher"):
        WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert len(calls) == 2, "the first foreign-key violation for the vanished folder ends the pass"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_disabling_a_folder_mid_pass_stops_the_walk_at_the_next_directory(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_main, "FOLDER_CHECK_INTERVAL_SECS", 0.0)
    root = _root(tmp_path)
    for d in ("d1", "d2", "d3"):
        (root / d).mkdir()
        for i in range(3):
            (root / d / f"f{i}.txt").write_text(f"{d} {i}")
    folder_id = _add_folder(factory, root)
    calls = []
    real_handle_event = watcher_main.handle_event

    def disabling_the_folder(session, event):
        calls.append(event.relative_path)
        if len(calls) == 1:
            other = factory()
            try:
                other.query(WatchedFolder).filter_by(folder_id=folder_id).one().enabled = False
                other.commit()
            finally:
                other.close()
        return real_handle_event(session, event)

    monkeypatch.setattr(watcher_main, "handle_event", disabling_the_folder)
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    dirs = {rel.split("/")[0] for rel in calls}
    assert len(dirs) == 1, f"the walk stopped after the directory it was in: {calls}"
    assert _last_scan_at(factory, folder_id) is None


# ---------------------------------------------------------------------------
# Seventh review of #860
# ---------------------------------------------------------------------------


def test_a_swap_through_a_temp_name_live_leaves_no_phantom(factory, tmp_path):
    """`mv a t; mv b a; mv t b` live: content ends right, nothing hidden stays
    searchable, and neither document is lost. Which path keeps which document
    depends on how FSEvents batches the renames: handled against the disk, a
    move whose source is back is compared by path (main's result: each path
    keeps its document)."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    folder_id = _add_folder(factory, root)
    with _Running(factory, folder_id) as run:
        run.wait_for(
            "both files indexed", lambda st: {k for k, v in st.items() if v[0] == ACTIVE} == {"a.txt", "b.txt"}
        )
        before = {k: v[1] for k, v in run.state().items()}
        (root / "a.txt").rename(root / "t.txt")
        (root / "b.txt").rename(root / "a.txt")
        (root / "t.txt").rename(root / "b.txt")

        def settled(st):
            active = {k: v for k, v in st.items() if v[0] == ACTIVE}
            return (
                set(active) == {"a.txt", "b.txt"}
                and active["a.txt"][3] == _sha("beta")
                and active["b.txt"][3] == _sha("alpha")
            )

        run.wait_for("the swap to settle with no phantom", settled)
        time.sleep(1.0)
        st = run.state()
    assert settled(st), st
    assert all(v[2] == "removed" for k, v in st.items() if v[0] == REMOVED), "nothing hidden stays searchable"
    assert {st["a.txt"][1], st["b.txt"][1]} == {before["a.txt"], before["b.txt"]}, "no document lost"


class _DeadOnArrival:
    """An observer whose restart returns but whose emitter is dead by the next check."""

    def __init__(self, root):
        self.root = root
        self.restarts = 0

    def health(self):
        from harbor_clerk.watcher.observer import RootState

        return RootState.detached

    def restart(self):
        self.restarts += 1


def test_an_emitter_that_dies_after_each_restart_is_retried_with_backoff(factory, tmp_path, caplog):
    import logging

    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)
    obs = _DeadOnArrival(str(tmp_path))
    daemon._observers[folder_id] = obs
    daemon._seen_stats[folder_id] = {"a.txt": (1, 2, 3, 4)}
    requested = []
    daemon._request_reconcile = requested.append

    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
        daemon._check_roots()  # first attach: one pass, the stat cache cleared once
        assert (obs.restarts, requested) == (1, [folder_id])
        assert folder_id not in daemon._seen_stats
        daemon._seen_stats[folder_id] = {"a.txt": (1, 2, 3, 4)}
        for _ in range(19):
            daemon._check_roots()
    # Restarts at checks 1, 3, 6, 11 and 20 (backoff after the 1st, 2nd, 3rd, 4th failure), not 20.
    assert obs.restarts <= 5, obs.restarts
    assert requested == [folder_id, folder_id], "one pass for the attach, one when the streak starts"
    assert daemon._seen_stats[folder_id] == {"a.txt": (1, 2, 3, 4)}, "retries keep the stat cache"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "stopped right after attaching" in warnings[0].getMessage(), "first: a warning"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "still not watching after a retry" in errors[0].getMessage(), "persisting: one error"


def test_a_real_observer_whose_emitter_dies_is_backed_off(factory, tmp_path, monkeypatch):
    """The reviewer's fault injection: every attach "succeeds" and its emitter ends at once."""
    from harbor_clerk.watcher.observer import FolderObserver

    root = _root(tmp_path)
    _add_folder(factory, root)
    real_start = FolderObserver.start
    starts = []

    def start_then_die(self):
        real_start(self)
        starts.append(1)
        for emitter in list(self._observer.emitters):
            emitter.stop()
            emitter.join(timeout=5.0)

    monkeypatch.setattr(FolderObserver, "start", start_then_die)
    daemon = WatcherDaemon(factory)
    requested = []
    daemon._request_reconcile = requested.append
    try:
        daemon._sync_observers()
        for _ in range(20):
            daemon._check_roots()
        assert len(starts) <= 6, f"{len(starts)} attaches in 20 checks"
        assert len(requested) == 2, requested
    finally:
        daemon.stop()


def test_an_aborted_pass_is_retried_with_backoff_whatever_the_interval(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_main, "RETRY_FIRST_SECS", 30.0)
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    real_active_paths = daemon._active_paths

    def unreachable(_folder_id):
        raise _outage()

    daemon._active_paths = unreachable
    daemon._reconcile_folder(folder_id, str(root))
    assert daemon._retry_delay[folder_id] == 30.0
    daemon._reconcile_folder(folder_id, str(root))
    assert daemon._retry_delay[folder_id] == 60.0, "doubling"
    for _ in range(5):
        daemon._reconcile_folder(folder_id, str(root))
    assert daemon._retry_delay[folder_id] == watcher_main.RETRY_MAX_SECS, "capped"

    requested = []
    real_request = daemon._request_reconcile
    daemon._request_reconcile = requested.append
    daemon._run_due_retries()
    assert requested == [], "not due yet"
    daemon._retry_at[folder_id] = 0.0
    daemon._run_due_retries()
    assert requested == [folder_id]
    daemon._request_reconcile = real_request

    daemon._active_paths = real_active_paths
    daemon._reconcile_folder(folder_id, str(root))
    assert folder_id not in daemon._retry_delay and folder_id not in daemon._retry_at, "a completed pass ends it"


def test_with_the_interval_at_zero_an_outage_at_attach_still_ends_scanning(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_main, "RETRY_FIRST_SECS", 0.3)
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    failures = []
    real_active_paths = WatcherDaemon._active_paths

    def unreachable_once(self, fid):
        if not failures:
            failures.append(1)
            raise _outage()
        return real_active_paths(self, fid)

    monkeypatch.setattr(WatcherDaemon, "_active_paths", unreachable_once)
    with _Running(factory, folder_id) as run:  # waits, bounded, for last_scan_at
        assert failures == [1]
        assert list(run.state()) == ["a.txt"]


def test_an_unchanged_sidecar_is_not_read_again_on_the_next_pass(factory, tmp_path, monkeypatch):
    import harbor_clerk.watcher.events as ev

    root = _root(tmp_path)
    for i in range(5):
        (root / f"INV-{i}.pdf").write_bytes(b"%PDF-1.4 " + str(i).encode())
        (root / f"INV-{i}.json").write_text('{"vendor": "Acme"}')
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))

    reads = []
    for module in (ev, watcher_main):
        real = module.load_sidecar
        monkeypatch.setattr(
            module, "load_sidecar", lambda path, *a, _real=real, **k: reads.append(str(path)) or _real(path, *a, **k)
        )
    daemon._reconcile_folder(folder_id, str(root))
    assert reads == [], "an unchanged sidecar is answered from its stat"

    (root / "INV-2.json").write_text('{"vendor": "Acme Corporation"}')
    daemon._reconcile_folder(folder_id, str(root))
    assert reads == [str(root / "INV-2.json")]
    assert len(_state(factory, folder_id)) == 5, "the sidecars are still sidecars"


# ---------------------------------------------------------------------------
# Eighth review of #860
# ---------------------------------------------------------------------------


def _seed_subtree(factory, tmp_path):
    root = _root(tmp_path)
    (root / "top.txt").write_text("top")
    (root / "sub").mkdir()
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "a.txt").write_text("alpha")
    (root / "sub" / "deep" / "b.txt").write_text("beta")
    folder_id = _reconcile_once(factory, root)
    return root, folder_id


def test_an_emptied_subdirectory_is_left_alone_but_a_removed_one_is_not(factory, tmp_path, caplog):
    """#868: an unmounted mount point below the root is an empty directory, so
    its rows stay (logged once). A subdirectory that is gone altogether is
    reconciled as before, and so is one that still holds an entry: here sub/
    holds the empty deep/, so sub/a.txt is retired and only deep/ is held. The
    accepted cost: `rm sub/*` while the watcher is down, leaving sub/ with no
    entries at all, is held the same way."""
    import logging

    root, folder_id = _seed_subtree(factory, tmp_path)
    (root / "gone").mkdir()
    (root / "gone" / "c.txt").write_text("gamma")
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))

    (root / "sub" / "a.txt").unlink()
    (root / "sub" / "deep" / "b.txt").unlink()
    (root / "gone" / "c.txt").unlink()
    (root / "gone").rmdir()
    daemon = WatcherDaemon(factory)
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.watcher"):
        daemon._reconcile_folder(folder_id, str(root))
        daemon._reconcile_folder(folder_id, str(root))
    assert len([r for r in caplog.records if "left sub/deep alone" in r.getMessage()]) == 1, "logged once"
    st = _state(factory, folder_id)
    assert st["sub/a.txt"][0:3:2] == (REMOVED, "removed"), "sub/ holds an entry (deep/): reconciled"
    assert st["sub/deep/b.txt"][0:3:2] == (ACTIVE, "active")
    assert st["gone/c.txt"][0:3:2] == (REMOVED, "removed")
    assert st["top.txt"][0:3:2] == (ACTIVE, "active")

    (root / "sub" / "deep").rmdir()
    daemon._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["sub/deep/b.txt"][0:3:2] == (ACTIVE, "active"), "an empty sub/ holds it"

    held = root / "sub" / "deep" / "b.txt"
    daemon._on_event(watcher_main.FileEvent(watcher_main.EventKind.deleted, folder_id, "sub/deep/b.txt", str(held)))
    assert _state(factory, folder_id)["sub/deep/b.txt"][0:3:2] == (REMOVED, "removed"), "live deletes still retire"


@pytest.mark.parametrize(
    "beside",
    [[".DS_Store"], ["photo.heic"], [".DS_Store", "photo.heic"]],
    ids=["ds-store", "unsupported", "both"],
)
def test_a_delete_while_down_beside_only_noise_or_unsupported_files_is_retired(factory, tmp_path, beside):
    """Finder leaves a .DS_Store in almost every folder it shows, and an
    unsupported file is an entry too: a directory holding them is not an empty
    mount point, so a document deleted from it while down is retired."""
    root = _root(tmp_path)
    (root / "keep.txt").write_text("keep")
    (root / "docs").mkdir()
    (root / "docs" / "spec.pdf").write_bytes(b"%PDF-1.4 spec")
    for name in beside:
        (root / "docs" / name).write_bytes(b"x")
    folder_id = _reconcile_once(factory, root)
    assert _state(factory, folder_id)["docs/spec.pdf"][0] == ACTIVE

    (root / "docs" / "spec.pdf").unlink()
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["docs/spec.pdf"][0:3:2] == (REMOVED, "removed")


def test_a_database_outage_while_recording_the_pass_retries_it(factory, _sync_engine, tmp_path):
    import psycopg2
    from sqlalchemy import event as sa_event

    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    failed = []

    def fail_the_first_stamp(conn, cursor, statement, parameters, context, executemany):
        if not failed and statement.lstrip().upper().startswith("UPDATE WATCHED_FOLDERS"):
            failed.append(statement)
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    sa_event.listen(_sync_engine, "before_cursor_execute", fail_the_first_stamp)
    try:
        daemon._reconcile_folder(folder_id, str(root))
    finally:
        sa_event.remove(_sync_engine, "before_cursor_execute", fail_the_first_stamp)
    assert failed, "the hook fired"
    assert _last_scan_at(factory, folder_id) is None
    assert folder_id in daemon._retry_at, "an unrecorded pass is an aborted one: retried"

    daemon._reconcile_folder(folder_id, str(root))
    assert _last_scan_at(factory, folder_id) is not None
    assert folder_id not in daemon._retry_at


def test_a_move_while_down_keeps_its_document(factory, tmp_path):
    """#847: `mv a.txt sub/a.txt` while the watcher was down is one missing row
    and one new path of the same content: a move, not retire plus create."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["a.txt"][1]
    (root / "sub").mkdir()
    (root / "a.txt").rename(root / "sub" / "a.txt")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert "a.txt" not in st
    assert st["sub/a.txt"] == (ACTIVE, doc_id, "active", _sha("alpha"), 0)


def test_a_copy_while_down_is_not_paired(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["a.txt"][1]
    (root / "copy.txt").write_text("alpha")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["a.txt"][:3] == (ACTIVE, doc_id, "active")
    assert st["copy.txt"][0] == ACTIVE and st["copy.txt"][1] != doc_id


@pytest.mark.parametrize("side", ["two-new-paths", "two-missing-rows"])
def test_an_ambiguous_same_content_set_is_not_paired(factory, tmp_path, side):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    if side == "two-missing-rows":
        (root / "a2.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    old_ids = {k: v[1] for k, v in _state(factory, folder_id).items()}
    (root / "a.txt").unlink()
    if side == "two-missing-rows":
        (root / "a2.txt").unlink()
        (root / "new.txt").write_text("alpha")
    else:
        (root / "new1.txt").write_text("alpha")
        (root / "new2.txt").write_text("alpha")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["a.txt"][0:3:2] == (REMOVED, "removed")
    new_paths = [k for k in st if k.startswith("new")]
    assert all(st[k][0] == ACTIVE and st[k][1] not in old_ids.values() for k in new_paths), st


def test_a_root_that_is_back_is_not_re_passed_while_a_retry_is_pending(factory, tmp_path):
    from harbor_clerk.watcher.observer import RootState

    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)

    class Steady:
        root = str(tmp_path)

        def health(self):
            return RootState.ok

    daemon._observers[folder_id] = Steady()
    daemon._reasons[folder_id] = watcher_main.ROOT_MISSING
    daemon._retry_at[folder_id] = time.monotonic() + 60
    requested = []
    daemon._request_reconcile = requested.append
    daemon._check_roots()
    assert requested == [], "the pending retry's backoff applies"
    daemon._retry_at.pop(folder_id)
    daemon._check_roots()
    assert requested == [folder_id]


def test_an_unreadable_root_is_not_re_passed_every_check(factory, tmp_path):
    from harbor_clerk.watcher.observer import RootState

    root = _root(tmp_path)
    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)

    class Steady:
        def __init__(self, path):
            self.root = path

        def health(self):
            return RootState.ok

    daemon._observers[folder_id] = Steady(str(root))
    daemon._reasons[folder_id] = watcher_main.ROOT_UNREADABLE
    requested = []
    daemon._request_reconcile = requested.append
    root.chmod(0o300)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running with privileges that read a write-only directory")
        for _ in range(3):
            daemon._check_roots()
        assert requested == [], "still unreadable: nothing to compare"
    finally:
        root.chmod(0o755)
    daemon._check_roots()
    assert requested == [folder_id], "readable again: compared at once"


def test_the_monitor_logs_a_database_outage_once(factory, monkeypatch, caplog):
    import logging

    daemon = WatcherDaemon(factory)

    def unreachable():
        raise _outage()

    monkeypatch.setattr(daemon, "_sync_observers", unreachable)
    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
        for _ in range(4):
            daemon._monitor_tick(interval=0.001, next_rescan=0.0)
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "unreachable" in loud[0].getMessage(), [r.getMessage() for r in loud]
    assert not any("monitor pass failed" in r.getMessage() for r in caplog.records)


def test_an_invalidated_connection_counts_as_the_database_unreachable():
    from sqlalchemy.exc import DBAPIError

    invalidated = DBAPIError("SELECT 1", {}, Exception("connection reset"), connection_invalidated=True)
    other = DBAPIError("SELECT 1", {}, Exception("syntax error"), connection_invalidated=False)
    assert watcher_main._is_db_unreachable(invalidated)
    assert not watcher_main._is_db_unreachable(other)
    assert not watcher_main._is_db_unreachable(ValueError("not a database error"))


# ---------------------------------------------------------------------------
# Ninth review of #860
# ---------------------------------------------------------------------------


def test_a_file_the_database_refuses_does_not_stop_the_pass(factory, tmp_path, caplog):
    """A 6,000-character Message-ID makes its index row too large (SQLSTATE 54000,
    an OperationalError). That is the file's failure, not an outage: the pass
    logs it, goes on, applies its deletes and is stamped."""
    import logging

    root = _root(tmp_path)
    (root / "gone.txt").write_text("going")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    (root / "gone.txt").unlink()
    import secrets

    # Incompressible, so TOAST cannot shrink the index row under btree's 2,704-byte limit.
    message_id = "<" + secrets.token_hex(4000) + "@example.com>"
    (root / "huge.eml").write_bytes(
        (
            f"From: a@example.com\r\nTo: b@example.com\r\nSubject: s\r\nMessage-ID: {message_id}\r\n"
            "Date: Wed, 28 May 2026 12:00:00 +0000\r\n\r\nbody\r\n"
        ).encode()
    )
    daemon = WatcherDaemon(factory)
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.watcher"):
        daemon._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["gone.txt"][0:3:2] == (REMOVED, "removed"), "the pass went on and applied its delete"
    assert "huge.eml" not in st
    assert _last_scan_at(factory, folder_id) is not None
    assert folder_id not in daemon._retry_at
    assert (folder_id, "huge.eml") in daemon._event_failures, "a per-file failure"
    failed = [r for r in caplog.records if "huge.eml" in r.getMessage()]
    assert len(failed) == 1 and failed[0].exc_info


_CODED_ERRORS: dict = {}


def _driver_error(pgcode):
    """A psycopg2 OperationalError carrying ``pgcode`` (one class per SQLSTATE, as psycopg2 has)."""
    import psycopg2

    if pgcode not in _CODED_ERRORS:
        _CODED_ERRORS[pgcode] = type(f"Coded{pgcode}", (psycopg2.OperationalError,), {"pgcode": pgcode})
    return _CODED_ERRORS[pgcode]("driver error")


@pytest.mark.parametrize(
    ("pgcode", "outage"),
    [
        (None, True),
        ("08006", True),
        ("08001", True),
        ("54000", False),
        ("53100", False),
        ("57014", False),
        ("40001", False),
    ],
)
def test_only_a_connection_failure_counts_as_an_outage(pgcode, outage):
    from sqlalchemy.exc import OperationalError

    raw = _driver_error(pgcode)
    assert watcher_main._is_db_unreachable(raw) is outage
    assert watcher_main._is_db_unreachable(OperationalError("INSERT ...", {}, raw)) is outage


def test_a_connection_class_error_mid_pass_still_aborts_it(factory, tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    root = _root(tmp_path)
    for i in range(5):
        (root / f"f{i}.txt").write_text(f"file {i}")
    folder_id = _add_folder(factory, root)
    calls = []

    def connection_lost(session, event):
        calls.append(event.relative_path)
        raise OperationalError("INSERT ...", {}, _driver_error("08006"))

    monkeypatch.setattr(watcher_main, "handle_event", connection_lost)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    assert len(calls) == 1
    assert folder_id in daemon._retry_at


def test_failure_streak_keys_a_database_error_by_type_and_sqlstate():
    """SQLAlchemy's message embeds the statement and parameters: two failures
    of one kind that differ only in a uuid are the same failure."""
    import uuid as uuid_mod

    from sqlalchemy.exc import OperationalError

    streak = watcher_main.FailureStreak()

    def failing(parameter):
        return OperationalError("INSERT INTO documents VALUES (%(id)s)", {"id": parameter}, _driver_error("54000"))

    assert streak.record(failing(str(uuid_mod.uuid4()))) == "first"
    assert streak.record(failing(str(uuid_mod.uuid4()))) is None, "only the parameters differ"
    other = OperationalError("INSERT ...", {}, _driver_error("57014"))
    assert streak.record(other) == "changed", "another SQLSTATE is another failure"


def test_a_move_out_of_a_directory_it_leaves_empty_pairs(factory, tmp_path):
    """`mv sub/a.txt a.txt` while down leaves sub/ empty: the pairing runs
    before the #868 hold, so it is a move, not a new document plus a phantom."""
    root = _root(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["sub/a.txt"][1]
    (root / "sub" / "a.txt").rename(root / "a.txt")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert "sub/a.txt" not in st, "no phantom"
    assert st["a.txt"] == (ACTIVE, doc_id, "active", _sha("alpha"), 0)


def test_several_moves_out_of_a_directory_they_leave_empty_pair(factory, tmp_path):
    root = _root(tmp_path)
    (root / "sub").mkdir()
    for i in range(3):
        (root / "sub" / f"f{i}.txt").write_text(f"file {i}")
    folder_id = _reconcile_once(factory, root)
    before = {k: v[1] for k, v in _state(factory, folder_id).items()}
    (root / "other").mkdir()
    for i in range(3):
        (root / "sub" / f"f{i}.txt").rename(root / "other" / f"f{i}.txt")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert {k: (v[0], v[1]) for k, v in st.items()} == {
        f"other/f{i}.txt": (ACTIVE, before[f"sub/f{i}.txt"]) for i in range(3)
    }


def _count_hashes(monkeypatch):
    import harbor_clerk.watcher.events as ev

    hashed = []
    for module in (ev, watcher_main):
        real = module.sha256_of
        monkeypatch.setattr(module, "sha256_of", lambda path, _real=real: hashed.append(path) or _real(path))
    return hashed


def test_a_new_file_is_hashed_once_in_a_pass_that_pairs(factory, tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    (root / "a.txt").rename(root / "moved.txt")
    (root / "new.txt").write_text("a new file")
    hashed = _count_hashes(monkeypatch)

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert sorted(os.path.basename(p) for p in hashed) == ["keep.txt", "moved.txt", "new.txt"]


def test_a_paired_destination_is_not_hashed_again_on_the_next_pass(factory, tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    (root / "a.txt").rename(root / "moved.txt")
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    hashed = _count_hashes(monkeypatch)

    daemon._reconcile_folder(folder_id, str(root))
    assert hashed == [], "every file's stat was recorded by the pass that paired it"


def test_a_paired_move_that_fails_falls_back_to_retire_plus_create(factory, tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["a.txt"][1]
    (root / "a.txt").rename(root / "moved.txt")
    real_handle_event = watcher_main.handle_event

    def moves_fail(session, event):
        if event.kind == watcher_main.EventKind.moved:
            raise ValueError("the move could not be applied")
        return real_handle_event(session, event)

    monkeypatch.setattr(watcher_main, "handle_event", moves_fail)
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["a.txt"][0:3:2] == (REMOVED, "removed")
    assert st["moved.txt"][0] == ACTIVE and st["moved.txt"][1] != doc_id


def test_removing_a_folder_drops_its_held_subtrees_and_its_lock(factory, tmp_path):
    root = _root(tmp_path)
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)

    class Stub:
        def stop(self):
            pass

    daemon._observers[folder_id] = Stub()
    daemon._left_alone[folder_id] = {"sub"}
    daemon._folder_lock(folder_id)
    sess = factory()
    try:
        sess.query(WatchedFolder).filter_by(folder_id=folder_id).one().enabled = False
        sess.commit()
    finally:
        sess.close()
    daemon._sync_observers()
    assert folder_id not in daemon._left_alone
    assert folder_id not in daemon._folder_locks


# ---------------------------------------------------------------------------
# Tenth review of #860
# ---------------------------------------------------------------------------


def _mark_read_sidecar(factory, folder_id, rel, sidecar):
    """As the extract stage leaves a document whose sidecar it read."""
    sess = factory()
    try:
        wf = sess.query(WatchedFile).filter_by(folder_id=folder_id, relative_path=rel).one()
        doc = sess.get(Document, wf.doc_id)
        doc.doc_metadata = {"sidecar": sidecar}
        for job in sess.query(IngestionJob).filter_by(doc_id=doc.doc_id).all():
            sess.delete(job)
        sess.commit()
    finally:
        sess.close()


def test_a_directory_renamed_while_down_with_its_sidecars_is_not_re_extracted(factory, tmp_path):
    root = _root(tmp_path)
    (root / "proj").mkdir()
    for i in range(3):
        (root / "proj" / f"INV-{i}.pdf").write_bytes(b"%PDF-1.4 " + str(i).encode())
        (root / "proj" / f"INV-{i}.json").write_text(f'{{"vendor": "Acme {i}"}}')
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    for i in range(3):
        _mark_read_sidecar(factory, folder_id, f"proj/INV-{i}.pdf", {"vendor": f"Acme {i}"})
    before = {k: v[1] for k, v in _state(factory, folder_id).items()}
    (root / "proj").rename(root / "proj2023")

    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    for i in range(3):
        assert st[f"proj2023/INV-{i}.pdf"] == (
            ACTIVE,
            before[f"proj/INV-{i}.pdf"],
            "active",
            st[f"proj2023/INV-{i}.pdf"][3],
            0,
        )


def test_a_pairing_is_dropped_when_the_old_path_is_back(factory, tmp_path, monkeypatch):
    """The old file reappears between the walk and the pairing: the new path is
    a copy then, not the old file moved."""
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    (root / "keep.txt").write_text("keep")
    folder_id = _reconcile_once(factory, root)
    doc_id = _state(factory, folder_id)["a.txt"][1]
    (root / "a.txt").rename(root / "moved.txt")
    real_sha = watcher_main.sha256_of

    def recreating_the_old_file(path):
        if path.endswith("moved.txt") and not (root / "a.txt").exists():
            (root / "a.txt").write_text("alpha")
        return real_sha(path)

    monkeypatch.setattr(watcher_main, "sha256_of", recreating_the_old_file)
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    st = _state(factory, folder_id)
    assert st["a.txt"][:3] == (ACTIVE, doc_id, "active"), "the old file keeps its document"
    assert st["moved.txt"][0] == ACTIVE and st["moved.txt"][1] != doc_id


def test_a_vanished_sidecars_cache_entry_is_pruned(factory, tmp_path):
    root = _root(tmp_path)
    (root / "INV-1.pdf").write_bytes(b"%PDF-1.4 one")
    (root / "INV-1.json").write_text('{"vendor": "Acme"}')
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)
    daemon._reconcile_folder(folder_id, str(root))
    assert "INV-1.json" in daemon._sidecar_cache[folder_id]

    (root / "INV-1.json").unlink()
    daemon._reconcile_folder(folder_id, str(root))
    assert "INV-1.json" not in daemon._sidecar_cache[folder_id]
