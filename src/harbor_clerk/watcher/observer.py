"""Wraps watchdog.observers.Observer for one folder, with polling fallback."""

import logging
import os
import threading
import unicodedata
import uuid
from collections.abc import Callable
from enum import Enum

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from harbor_clerk.watcher.events import EventKind, FileEvent

logger = logging.getLogger(__name__)


class RootState(str, Enum):
    """What ``FolderObserver.health`` found at the watched root."""

    ok = "ok"
    # The root path does not exist (an unmounted volume, a folder moved aside
    # and not yet replaced). Nothing is re-attached and nothing is removed.
    missing = "missing"
    # The root exists but the observer is not watching it: a different
    # directory now sits at the path, the emitter stopped itself, or the
    # observer never started. Re-attach, then reconcile.
    detached = "detached"


def _root_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _root_spellings(root: str) -> list[str]:
    """The ways an event may spell ``root``: as given and resolved (FSEvents
    reports real paths, /private/var/... for /var/...), each also NFC (watchdog
    normalises the path it schedules)."""
    spellings = []
    for path in (root, os.path.realpath(root)):
        path = os.path.normpath(path)
        spellings += [path, unicodedata.normalize("NFC", path)]
    return list(dict.fromkeys(spellings))


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        folder_id: uuid.UUID,
        root: str,
        sink: Callable[[FileEvent], None],
        on_root_lost: Callable[[], None] | None = None,
    ):
        self.folder_id = folder_id
        self.root = root
        self.sink = sink
        self.on_root_lost = on_root_lost
        self._roots = _root_spellings(root)
        # Whether the directory at the root is still the one this observer
        # attached to; set by FolderObserver. Events are dropped while it is not.
        self.attached: Callable[[], bool] | None = None

    def _relative(self, path: str | bytes) -> str | None:
        """``path`` relative to the root with "/" separators, or None when it is outside (or is) the root.

        The part below the root is kept as the event spells it, as the scan's
        ``os.walk`` names are, so both reach the same ``WatchedFile`` row.
        """
        path = os.path.normpath(os.fsdecode(path))
        for root in self._roots:
            rel = os.path.relpath(path, root)
            if rel == "." or rel == os.pardir or rel.startswith(os.pardir + os.sep):
                continue
            return rel.replace(os.sep, "/")
        return None

    def _is_root(self, path: str | bytes) -> bool:
        path = os.path.normpath(os.fsdecode(path))
        return path in self._roots or unicodedata.normalize("NFC", path) in self._roots

    def _emit(self, kind: EventKind, src_path: str | bytes, *, is_directory: bool = False) -> None:
        rel = self._relative(src_path)
        if rel is None:
            return
        self._deliver(
            FileEvent(
                kind=kind,
                folder_id=self.folder_id,
                relative_path=rel,
                absolute_path=os.fsdecode(src_path),
                is_directory=is_directory,
            )
        )

    def _deliver(self, event: FileEvent) -> None:
        """Hand an event on, unless the root is missing or is no longer the directory attached to.

        inotify keeps a watch on a root moved aside and reports what happens in
        it under the old paths (watchdog never updates the path on
        IN_MOVE_SELF), so a delete in the moved-aside directory would retire
        documents, a whole subtree for a directory. FSEvents can deliver a
        batch after the root went. A missing root is left alone; the
        reconciliation after re-attach decides.
        """
        if self.attached is not None and not self.attached():
            logger.debug(
                "watcher: dropped %s %s: the root is not the directory attached to",
                event.kind.value,
                event.relative_path,
            )
            return
        self.sink(event)

    def _root_lost(self) -> None:
        # Never restart the observer from here: this runs on its own thread,
        # and stopping it would join that thread. The daemon re-attaches.
        logger.warning("watcher: watched root %s was deleted, moved or replaced", self.root)
        if self.on_root_lost is not None:
            self.on_root_lost()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._emit(EventKind.created, event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._emit(EventKind.modified, event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            if self._is_root(event.src_path):
                # Every backend reports a lost root this way, then stops.
                self._root_lost()
                return
            self._emit(EventKind.deleted, event.src_path, is_directory=True)
            return
        self._emit(EventKind.deleted, event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Renames and moves (#847). Moving out of the tree is a delete, moving in a create.

        A directory moved within the tree needs nothing here: every backend
        follows it with a moved event per file inside it.
        """
        if event.is_directory and self._is_root(event.src_path):
            self._root_lost()
            return
        src_rel = self._relative(event.src_path)
        dest_rel = self._relative(event.dest_path)
        if src_rel is None and dest_rel is None:
            return
        if event.is_directory:
            if dest_rel is None:
                self._emit(EventKind.deleted, event.src_path, is_directory=True)
            elif src_rel is None:
                for dirpath, _dirnames, filenames in os.walk(os.fsdecode(event.dest_path)):
                    for name in filenames:
                        self._emit(EventKind.created, os.path.join(dirpath, name))
            return
        if dest_rel is None:
            self._emit(EventKind.deleted, event.src_path)
            return
        if src_rel is None:
            self._emit(EventKind.created, event.dest_path)
            return
        self._deliver(
            FileEvent(
                kind=EventKind.moved,
                folder_id=self.folder_id,
                relative_path=src_rel,
                absolute_path=os.fsdecode(event.src_path),
                dest_relative_path=dest_rel,
                dest_absolute_path=os.fsdecode(event.dest_path),
            )
        )


class FolderObserver:
    """One observer per watched folder. Tries native first, falls back to polling.

    Keeps the identity (device, inode) of the root it attached to, so the
    daemon can tell when the directory at the path has been replaced (#739):
    watchdog's emitters stop themselves when the root goes, and none of them
    picks up a new directory created at the same path.
    """

    def __init__(
        self,
        folder_id: uuid.UUID,
        root: str,
        sink: Callable[[FileEvent], None],
        on_root_lost: Callable[[], None] | None = None,
    ):
        self.folder_id = folder_id
        self.root = root
        self._root_lost = threading.Event()
        self._on_root_lost = on_root_lost
        self._handler = _Handler(folder_id, root, sink, self._note_root_lost)
        self._handler.attached = self._root_is_attached
        self._observer: Observer | PollingObserver | None = None
        self._root_id: tuple[int, int] | None = None

    def _root_is_attached(self) -> bool:
        """One stat: is the directory at the root the one attached to? True before any attach."""
        if self._root_id is None:
            return True
        return _root_identity(self.root) == self._root_id

    def _note_root_lost(self) -> None:
        self._root_lost.set()
        if self._on_root_lost is not None:
            self._on_root_lost()

    def start(self) -> None:
        self._root_lost.clear()
        self._root_id = _root_identity(self.root)
        try:
            obs = Observer()
            obs.schedule(self._handler, self.root, recursive=True)
            obs.start()
            self._observer = obs
            logger.info("watcher: native observer started for %s", self.root)
        except Exception:
            logger.warning(
                "watcher: native observer failed for %s, falling back to polling",
                self.root,
                exc_info=True,
            )
            obs = PollingObserver()
            obs.schedule(self._handler, self.root, recursive=True)
            obs.start()
            self._observer = obs

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def health(self) -> RootState:
        """Whether the observer is watching the directory now at ``root``. Cheap: one stat."""
        current = _root_identity(self.root)
        if current is None:
            return RootState.missing
        obs = self._observer
        if obs is None or not obs.is_alive():
            return RootState.detached
        if current != self._root_id or self._root_lost.is_set():
            return RootState.detached
        emitters = obs.emitters
        if not emitters or any(not emitter.is_alive() for emitter in emitters):
            return RootState.detached
        return RootState.ok
