import uuid

from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from harbor_clerk.watcher.events import EventKind
from harbor_clerk.watcher.observer import FolderObserver


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
