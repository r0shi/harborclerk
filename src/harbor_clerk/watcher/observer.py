"""Wraps watchdog.observers.Observer for one folder, with polling fallback."""

import logging
import os
import uuid
from collections.abc import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from harbor_clerk.watcher.events import EventKind, FileEvent

logger = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, folder_id: uuid.UUID, root: str, sink: Callable[[FileEvent], None]):
        self.folder_id = folder_id
        self.root = root
        self.sink = sink

    def _emit(self, kind: EventKind, src_path: str) -> None:
        rel = os.path.relpath(src_path, self.root)
        if rel.startswith(".."):
            return
        self.sink(
            FileEvent(
                kind=kind,
                folder_id=self.folder_id,
                relative_path=rel,
                absolute_path=src_path,
            )
        )

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
            return
        self._emit(EventKind.deleted, event.src_path)


class FolderObserver:
    """One observer per watched folder. Tries native first, falls back to polling."""

    def __init__(self, folder_id: uuid.UUID, root: str, sink: Callable[[FileEvent], None]):
        self.folder_id = folder_id
        self.root = root
        self._handler = _Handler(folder_id, root, sink)
        self._observer: Observer | PollingObserver | None = None

    def start(self) -> None:
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
