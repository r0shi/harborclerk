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


def test_reconcile_of_a_missing_root_removes_nothing(factory, tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    root.rename(tmp_path / "aside")
    WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    assert _state(factory, folder_id)["a.txt"][0:3:2] == (ACTIVE, "active")


def test_reconcile_with_an_unreadable_directory_removes_nothing(factory, tmp_path):
    root = _root(tmp_path)
    (root / "top.txt").write_text("top")
    sub = root / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("alpha")
    folder_id = _reconcile_once(factory, root)
    sub.chmod(0)
    try:
        if os.access(sub, os.R_OK):
            pytest.skip("running with privileges that read a mode-0 directory")
        WatcherDaemon(factory)._reconcile_folder(folder_id, str(root))
    finally:
        sub.chmod(0o755)
    assert _state(factory, folder_id)["sub/a.txt"][0:3:2] == (ACTIVE, "active")


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
