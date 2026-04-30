"""harbor-clerk-watcher entry point.

Lifecycle:
  - Loads enabled watched_folders rows from DB at start.
  - Spawns one FolderObserver per folder.
  - Runs Docker auto-discovery loop (no-op when WATCH_ROOT is empty).
  - Listens on PostgreSQL NOTIFY watched_folders_changed to pick up live
    folder add/remove/enable/disable from the API without restart.
"""

import logging
import signal
import sys
import threading
import time
import uuid

from sqlalchemy.orm import sessionmaker

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_engine
from harbor_clerk.log_setup import setup_logging
from harbor_clerk.models.watched import WatchedFolder
from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.watcher.events import FileEvent, handle_event
from harbor_clerk.watcher.observer import FolderObserver

logger = logging.getLogger(__name__)

DISCOVERY_INTERVAL_SECS = 60.0
NOTIFY_POLL_INTERVAL_SECS = 1.0


class WatcherDaemon:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory
        self._observers: dict[uuid.UUID, FolderObserver] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _on_event(self, event: FileEvent) -> None:
        sess = self._session_factory()
        try:
            handle_event(sess, event)
            sess.commit()
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to handle event %r", event)
        finally:
            sess.close()

    def _sync_observers(self) -> None:
        sess = self._session_factory()
        try:
            rows = sess.query(WatchedFolder).filter_by(enabled=True).all()
            wanted = {row.folder_id: row.path for row in rows}
        finally:
            sess.close()

        # Stop observers no longer wanted
        for fid in list(self._observers):
            if fid not in wanted:
                self._observers[fid].stop()
                del self._observers[fid]
                logger.info("watcher: stopped observer for folder %s", fid)

        # Start new observers
        for fid, path in wanted.items():
            if fid not in self._observers:
                obs = FolderObserver(fid, path, self._on_event)
                try:
                    obs.start()
                    self._observers[fid] = obs
                except Exception:
                    logger.exception("watcher: failed to start observer for %s (%s)", fid, path)

    def _discovery_loop(self) -> None:
        watch_root = get_settings().watch_root
        if not watch_root:
            return
        while not self._stop.is_set():
            sess = self._session_factory()
            try:
                scan_watch_root(sess, watch_root)
                sess.commit()
            except Exception:
                sess.rollback()
                logger.exception("watcher: discovery scan failed")
            finally:
                sess.close()
            self._sync_observers()
            self._stop.wait(timeout=DISCOVERY_INTERVAL_SECS)

    def _listener_loop(self) -> None:
        try:
            for _payload in listen_for_folder_changes(self._stop, poll_interval=NOTIFY_POLL_INTERVAL_SECS):
                self._sync_observers()
        except Exception:
            logger.exception("watcher: listener loop crashed")

    def start(self) -> None:
        self._sync_observers()
        for target in (self._discovery_loop, self._listener_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for obs in self._observers.values():
            obs.stop()
        self._observers.clear()
        for t in self._threads:
            t.join(timeout=5.0)
        self._threads.clear()


def main() -> None:
    setup_logging("watcher")
    logger.info("harbor-clerk-watcher starting")
    factory = sessionmaker(bind=get_sync_engine(), expire_on_commit=False)
    daemon = WatcherDaemon(factory)

    def _shutdown(signum, _frame):
        logger.info("harbor-clerk-watcher: signal %s, shutting down", signum)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    daemon.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        daemon.stop()
