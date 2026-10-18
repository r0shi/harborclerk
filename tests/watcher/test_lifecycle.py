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
from tests.watcher._fs import case_insensitive_only

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
    assert (obs.restarts, requested) == (1, []), "no pass over a root the observer is not watching"
    assert folder_id in daemon._seen_stats

    daemon._check_roots()
    assert (obs.restarts, requested) == (2, [folder_id])
    assert folder_id not in daemon._seen_stats, "a new attachment compares every file again"

    daemon._check_roots()
    assert (obs.restarts, requested) == (2, [folder_id]), "a healthy observer is left alone"
