import uuid

import pytest
from watchdog.events import (
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from harbor_clerk.watcher.events import EventKind
from harbor_clerk.watcher.observer import FolderObserver, RootState


def test_translates_watchdog_create(tmp_path):
    captured = []
    folder_id = uuid.uuid4()
    obs = FolderObserver(folder_id, str(tmp_path), captured.append)
    obs._handler.on_created(FileCreatedEvent(str(tmp_path / "a.txt")))
    assert len(captured) == 1
    assert captured[0].kind == EventKind.created
    assert captured[0].relative_path == "a.txt"
    assert captured[0].folder_id == folder_id


def test_translates_watchdog_modify(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_modified(FileModifiedEvent(str(tmp_path / "sub" / "b.txt")))
    assert captured[0].kind == EventKind.modified
    # On macOS the relpath separator is "/"; on Linux too. So expecting "sub/b.txt".
    assert captured[0].relative_path == "sub/b.txt"


def test_translates_watchdog_delete(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_deleted(FileDeletedEvent(str(tmp_path / "c.txt")))
    assert captured[0].kind == EventKind.deleted


def test_directory_events_ignored(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    e = FileCreatedEvent(str(tmp_path / "subdir"))
    e.is_directory = True
    obs._handler.on_created(e)
    assert captured == []


def test_paths_outside_root_ignored(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    # A path that, when relpath'd, starts with ".." (outside root)
    outside = tmp_path.parent / "elsewhere.txt"
    obs._handler.on_created(FileCreatedEvent(str(outside)))
    assert captured == []


def test_dotdot_prefixed_name_at_the_root_is_inside(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_created(FileCreatedEvent(str(tmp_path / "..notes.txt")))
    assert [e.relative_path for e in captured] == ["..notes.txt"]


def test_move_within_the_tree_is_a_moved_event(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_moved(FileMovedEvent(str(tmp_path / "a.txt"), str(tmp_path / "sub" / "b.txt")))
    assert len(captured) == 1
    e = captured[0]
    assert (e.kind, e.relative_path, e.dest_relative_path) == (EventKind.moved, "a.txt", "sub/b.txt")
    assert e.dest_absolute_path == str(tmp_path / "sub" / "b.txt")


def test_move_out_of_the_tree_is_a_delete_and_into_it_a_create(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(root), captured.append)
    obs._handler.on_moved(FileMovedEvent(str(root / "a.txt"), str(tmp_path / "a.txt")))
    obs._handler.on_moved(FileMovedEvent(str(tmp_path / "b.txt"), str(root / "b.txt")))
    assert [(e.kind, e.relative_path) for e in captured] == [
        (EventKind.deleted, "a.txt"),
        (EventKind.created, "b.txt"),
    ]


def test_directory_moved_out_is_a_directory_delete(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(root), captured.append)
    obs._handler.on_moved(DirMovedEvent(str(root / "sub"), str(tmp_path / "sub")))
    obs._handler.on_deleted(DirDeletedEvent(str(root / "gone")))
    assert [(e.kind, e.relative_path, e.is_directory) for e in captured] == [
        (EventKind.deleted, "sub", True),
        (EventKind.deleted, "gone", True),
    ]


def test_directory_moved_within_the_tree_emits_nothing_itself(tmp_path):
    """Every backend follows a directory move with a moved event per file inside."""
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_moved(DirMovedEvent(str(tmp_path / "a"), str(tmp_path / "b")))
    assert captured == []


def test_losing_the_root_reports_it_and_emits_no_file_events(tmp_path):
    """A deleted or moved root must never reach handle_event: that would
    remove every document of a folder that may only be unmounted (#739)."""
    captured, lost = [], []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append, on_root_lost=lambda: lost.append(1))
    obs._handler.on_deleted(DirDeletedEvent(str(tmp_path)))
    obs._handler.on_moved(DirMovedEvent(str(tmp_path), str(tmp_path.parent / "aside")))
    assert captured == []
    assert lost == [1, 1]
    assert obs._root_lost.is_set()


def test_health_tells_missing_from_replaced(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    obs = FolderObserver(uuid.uuid4(), str(root), lambda _e: None)
    assert obs.health() is RootState.detached, "never started"
    obs.start()
    try:
        assert obs.health() is RootState.ok
        root.rename(tmp_path / "aside")
        assert obs.health() is RootState.missing
        root.mkdir()
        assert obs.health() is RootState.detached, "a different directory at the path"
        obs.restart()
        assert obs.health() is RootState.ok
    finally:
        obs.stop()


def test_a_lost_root_reads_detached_even_while_its_emitter_still_runs(tmp_path):
    """The root-lost event can arrive before the emitter stops, or with the
    root back under the same inode: the flag alone must make it re-attach."""
    root = tmp_path / "root"
    root.mkdir()
    obs = FolderObserver(uuid.uuid4(), str(root), lambda _e: None)
    obs.start()
    try:
        assert obs.health() is RootState.ok
        obs._handler.on_deleted(DirDeletedEvent(str(root)))
        assert obs.health() is RootState.detached
        obs.restart()
        assert obs.health() is RootState.ok, "a fresh attach clears the flag"
    finally:
        obs.stop()


@pytest.mark.parametrize("how", ["moved-aside", "replaced"])
def test_events_are_dropped_while_the_root_is_not_the_directory_attached_to(tmp_path, how):
    """inotify keeps watching a root moved aside and reports its contents under
    the old paths; a delete there must not retire documents (or a subtree)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(root), captured.append)
    obs.start()
    try:
        root.rename(tmp_path / "aside")
        if how == "replaced":
            root.mkdir()
        obs._handler.on_deleted(FileDeletedEvent(str(root / "a.txt")))
        obs._handler.on_deleted(DirDeletedEvent(str(root / "sub")))
        obs._handler.on_moved(FileMovedEvent(str(root / "b.txt"), str(tmp_path / "elsewhere.txt")))
        obs._handler.on_created(FileCreatedEvent(str(root / "c.txt")))
        assert captured == []

        if how == "moved-aside":
            (tmp_path / "aside").rename(root)  # back, same inode: events flow again
            obs._handler.on_deleted(FileDeletedEvent(str(root / "a.txt")))
            assert [(e.kind, e.relative_path) for e in captured] == [(EventKind.deleted, "a.txt")]
    finally:
        obs.stop()


def test_an_emitter_that_died_after_start_reads_detached(tmp_path):
    """watchdog's FSEvents run() catches its own add_watch/read_events failures,
    so start() returns while the emitter thread has already ended."""
    root = tmp_path / "root"
    root.mkdir()
    obs = FolderObserver(uuid.uuid4(), str(root), lambda _e: None)
    obs.start()
    try:
        assert obs.health() is RootState.ok
        for emitter in list(obs._observer.emitters):
            emitter.stop()
            emitter.join(timeout=5.0)
            assert not emitter.is_alive()
        assert obs._observer.is_alive(), "the observer thread itself is still running"
        assert obs.health() is RootState.detached
    finally:
        obs.stop()
