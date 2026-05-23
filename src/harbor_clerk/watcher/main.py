"""harbor-clerk-watcher entry point.

Lifecycle:
  - Loads enabled watched_folders rows from DB at start.
  - Spawns one FolderObserver per folder.
  - Runs Docker auto-discovery loop (no-op when WATCH_ROOT is empty).
  - Listens on PostgreSQL NOTIFY watched_folders_changed to pick up live
    folder add/remove/enable/disable from the API without restart.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_engine
from harbor_clerk.log_setup import setup_logging
from harbor_clerk.models.watched import WatchedFolder
from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.watcher.events import EventKind, FileEvent, SkipReason, classify_skip, handle_event
from harbor_clerk.watcher.mail_observer import MailObserver
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
        self._mail_thread: threading.Thread | None = None
        self._mail_observer: MailObserver | None = None
        self._mail_loop: asyncio.AbstractEventLoop | None = None

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
                    continue
                # Run an initial scan in a daemon thread so the sync loop
                # doesn't block on it. The observer is already registered
                # so any files added DURING the scan get caught by both
                # paths — handle_event's sha-based dedup makes the overlap
                # a no-op.
                threading.Thread(
                    target=self._scan_folder,
                    args=(fid, path),
                    daemon=True,
                    name=f"initial-scan-{str(fid)[:8]}",
                ).start()

    def _scan_folder(self, folder_id: uuid.UUID, root: str) -> None:
        """Walk an existing folder and emit synthetic `created` events for
        every file already on disk. Required because watchdog only delivers
        events for changes that happen AFTER the observer starts — files
        already in the folder when it was added would never be ingested.

        Updates `watched_folders.last_scan_at` when done so the API's
        scan_status flips from "scanning" to "idle". Also tallies the
        per-folder ``skipped_count`` + ``skipped_extensions`` from files
        the watcher rejected for an unsupported extension (NOT counting
        intentional-noise filters: dotfiles, AppleDouble, __MACOSX,
        Excalidraw).
        """
        logger.info("watcher: initial scan of %s starting", root)
        count = 0
        skipped_count = 0
        skipped_exts: set[str] = set()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune dotfile / __MACOSX directories so we don't recurse
                # into them — both for speed and to avoid the cost of
                # invoking _on_event for every file in a .git tree.
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
                for fname in filenames:
                    abs_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")

                    # Classify BEFORE handing off to _on_event so we can
                    # tally UNSUPPORTED_EXTENSION skips without going
                    # through the event-handler path twice.
                    reason = classify_skip(rel_path)
                    if reason is SkipReason.UNSUPPORTED_EXTENSION:
                        skipped_count += 1
                        suffix = os.path.splitext(rel_path)[1].lower()
                        # Extension-less files (README, LICENSE, …) are
                        # legitimately UNSUPPORTED but `splitext` returns
                        # ""; track them under a sentinel so they show up
                        # in the per-folder UI summary instead of leaving
                        # the user wondering why the count went up but
                        # no new extension appeared.
                        skipped_exts.add(suffix if suffix else "(no extension)")
                        # Don't dispatch the event — handle_event would
                        # ignore it anyway, but skipping the call saves
                        # the SHA computation and DB roundtrip.
                        continue
                    if reason is SkipReason.NOISE:
                        # Noise filters are intentional; don't tally and
                        # don't dispatch.
                        continue

                    self._on_event(
                        FileEvent(
                            kind=EventKind.created,
                            folder_id=folder_id,
                            relative_path=rel_path,
                            absolute_path=abs_path,
                        )
                    )
                    count += 1
                    if self._stop.is_set():
                        # Early stop: partial skip tally is silently
                        # discarded along with last_scan_at — both will
                        # be repopulated by a fresh _scan_folder on the
                        # next daemon start. Acceptable because scan
                        # interruption is rare and the next start re-runs
                        # the whole walk.
                        return
        except Exception:
            logger.exception("watcher: initial scan of %s failed", root)
            return

        # Mark scan complete and write the skip tally on the folder row.
        sess = self._session_factory()
        try:
            folder = sess.query(WatchedFolder).filter_by(folder_id=folder_id).one_or_none()
            if folder is not None:
                folder.last_scan_at = datetime.now(UTC)
                folder.skipped_count = skipped_count
                folder.skipped_extensions = sorted(skipped_exts)
                sess.commit()
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to update last_scan_at for %s", folder_id)
        finally:
            sess.close()

        logger.info(
            "watcher: initial scan of %s complete (%d files visited, %d skipped — extensions=%s)",
            root,
            count,
            skipped_count,
            sorted(skipped_exts),
        )

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
        # We don't need the payload itself — any NOTIFY arrival means a folder
        # row changed; _sync_observers() reads the current world from DB.
        try:
            for _ in listen_for_folder_changes(self._stop, poll_interval=NOTIFY_POLL_INTERVAL_SECS):
                self._sync_observers()
        except Exception:
            logger.exception("watcher: listener loop crashed")

    def start(self) -> None:
        self._sync_observers()
        for target in (self._discovery_loop, self._listener_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

        def _run_mail_observer() -> None:
            self._mail_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._mail_loop)
            self._mail_observer = MailObserver()
            try:
                self._mail_loop.run_until_complete(self._mail_observer.run())
            except Exception:
                logger.exception("watcher: mail observer exited with error — filesystem watcher continues")
            finally:
                self._mail_loop.close()

        self._mail_thread = threading.Thread(target=_run_mail_observer, name="mail-observer", daemon=True)
        self._mail_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._mail_loop is not None and self._mail_observer is not None and self._mail_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._mail_observer.stop(), self._mail_loop).result(timeout=10)
            except Exception:
                logger.exception("watcher: error stopping mail observer")
        if self._mail_thread is not None:
            self._mail_thread.join(timeout=10)
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
