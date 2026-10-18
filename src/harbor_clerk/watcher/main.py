"""harbor-clerk-watcher entry point.

Lifecycle:
  - Loads enabled watched_folders rows from DB at start.
  - Spawns one FolderObserver per folder, and reconciles each folder with
    the index when its observer attaches and every WATCH_RESCAN_INTERVAL_SECS.
  - Re-attaches an observer whose root was replaced (checked every few seconds).
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
from harbor_clerk.error_text import describe_error
from harbor_clerk.log_setup import setup_logging
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.watcher.events import EventKind, FileEvent, SkipReason, classify_skip, handle_event
from harbor_clerk.watcher.failure_streak import FailureStreak
from harbor_clerk.watcher.mail_observer import MailObserver
from harbor_clerk.watcher.observer import FolderObserver, RootState

logger = logging.getLogger(__name__)

DISCOVERY_INTERVAL_SECS = 60.0
NOTIFY_POLL_INTERVAL_SECS = 1.0
# Reconnect backoff for the folder-change listener (#593). One dropped LISTEN
# connection (a Postgres restart) used to end the listener thread for good, and
# live folder add/remove/enable/disable from the API stopped taking effect until
# the watcher restarted.
LISTENER_RETRY_BASE_SECS = 2.0
LISTENER_RETRY_MAX_SECS = 60.0
# A LISTEN connection that stayed up this long was working; its loss starts a new
# failure streak at the base delay rather than continuing the old one.
LISTENER_HEALTHY_SECS = 120.0
# How often each watched root is stat'ed to notice it was replaced (#739). An
# observer that sees its root go wakes the check at once; this is the backstop.
ROOT_CHECK_INTERVAL_SECS = 5.0

StatKey = tuple[int, int, int, int, int]


def _stat_key(path: str) -> StatKey | None:
    """What a reconciliation compares to call a file unchanged. ctime moves on any write and cannot be set."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class WatcherDaemon:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory
        self._observers: dict[uuid.UUID, FolderObserver] = {}
        # _sync_observers runs on the discovery, listener and monitor threads.
        self._observers_lock = threading.RLock()
        # One event at a time per folder: the observer thread and a
        # reconciliation both dispatch, and two creates of one new file would
        # race into the same (folder_id, relative_path).
        self._folder_locks: dict[uuid.UUID, threading.Lock] = {}
        self._folder_locks_guard = threading.Lock()
        # At most one reconciliation per folder; one requested while another
        # runs makes it run once more when it finishes.
        self._reconcile_guard = threading.Lock()
        self._reconciling: set[uuid.UUID] = set()
        self._reconcile_again: set[uuid.UUID] = set()
        # Per folder, the stat each file had when a reconciliation last
        # compared it with an active row; an unchanged stat is not hashed again.
        self._seen_stats: dict[uuid.UUID, dict[str, StatKey]] = {}
        self._missing_roots: set[uuid.UUID] = set()
        self._stop = threading.Event()
        # Set by an observer that lost its root, so the monitor checks now.
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._mail_thread: threading.Thread | None = None
        self._mail_observer: MailObserver | None = None
        self._mail_loop: asyncio.AbstractEventLoop | None = None

    def _folder_lock(self, folder_id: uuid.UUID) -> threading.Lock:
        with self._folder_locks_guard:
            return self._folder_locks.setdefault(folder_id, threading.Lock())

    def _on_event(self, event: FileEvent) -> bool:
        """Apply one event in its own transaction. Returns False when it failed (logged)."""
        with self._folder_lock(event.folder_id):
            sess = self._session_factory()
            try:
                handle_event(sess, event)
                sess.commit()
                return True
            except Exception:
                sess.rollback()
                logger.exception("watcher: failed to handle event %r", event)
                return False
            finally:
                sess.close()

    def _sync_observers(self) -> None:
        sess = self._session_factory()
        try:
            rows = sess.query(WatchedFolder).filter_by(enabled=True).all()
            wanted = {row.folder_id: row.path for row in rows}
        finally:
            sess.close()

        with self._observers_lock:
            # Stop observers no longer wanted
            for fid in list(self._observers):
                if fid not in wanted:
                    self._observers[fid].stop()
                    del self._observers[fid]
                    self._seen_stats.pop(fid, None)
                    self._missing_roots.discard(fid)
                    logger.info("watcher: stopped observer for folder %s", fid)

            # Start new observers. One that cannot start (its root is missing)
            # stays registered, and the monitor attaches it when the root appears.
            for fid, path in wanted.items():
                if fid in self._observers:
                    continue
                obs = FolderObserver(fid, path, self._on_event, on_root_lost=self._wake.set)
                self._observers[fid] = obs
                try:
                    obs.start()
                except Exception:
                    logger.exception("watcher: failed to start observer for %s (%s)", fid, path)
                    continue
                # Reconcile in a daemon thread so the sync loop doesn't block
                # on it. The observer is already registered, so a file added
                # during the walk is caught by both; the sha comparison makes
                # the overlap a no-op.
                self._request_reconcile(fid)

    def _check_roots(self) -> None:
        """Re-attach every observer whose root was replaced or whose emitter stopped (#739), then reconcile.

        A missing root is left alone: nothing is re-attached and nothing is
        removed until it is back (an unmounted volume returns as it was).
        """
        with self._observers_lock:
            observers = list(self._observers.items())
        for fid, obs in observers:
            state = obs.health()
            if state is RootState.missing:
                if fid not in self._missing_roots:
                    self._missing_roots.add(fid)
                    logger.warning("watcher: root of folder %s is missing (%s); waiting for it", fid, obs.root)
                continue
            self._missing_roots.discard(fid)
            if state is RootState.ok:
                continue
            with self._observers_lock:
                if self._observers.get(fid) is not obs:
                    continue
                logger.info("watcher: re-attaching observer for %s", obs.root)
                try:
                    obs.restart()
                except Exception:
                    logger.exception("watcher: failed to re-attach observer for %s", obs.root)
                    continue
                self._seen_stats.pop(fid, None)
            self._request_reconcile(fid)

    def _request_reconcile(self, folder_id: uuid.UUID) -> None:
        with self._reconcile_guard:
            if folder_id in self._reconciling:
                self._reconcile_again.add(folder_id)
                return
            self._reconciling.add(folder_id)
        threading.Thread(
            target=self._reconcile_worker,
            args=(folder_id,),
            daemon=True,
            name=f"reconcile-{str(folder_id)[:8]}",
        ).start()

    def _reconcile_worker(self, folder_id: uuid.UUID) -> None:
        try:
            while not self._stop.is_set():
                with self._observers_lock:
                    obs = self._observers.get(folder_id)
                if obs is not None:
                    self._reconcile_folder(folder_id, obs.root)
                with self._reconcile_guard:
                    if folder_id not in self._reconcile_again:
                        break
                    self._reconcile_again.discard(folder_id)
        finally:
            with self._reconcile_guard:
                self._reconciling.discard(folder_id)
                self._reconcile_again.discard(folder_id)

    def _reconcile_folder(self, folder_id: uuid.UUID, root: str) -> None:
        """Compare a folder on disk with the index, both ways (#847, #739).

        Every admitted file is dispatched as a `created` event (the sha
        comparison makes a known, unchanged file a no-op, indexes a new one,
        reprocesses a changed one and resurrects a removed one), except a file
        whose stat is unchanged since this process last compared it with an
        active row. Then every active row whose file is gone is dispatched as
        a delete, which `handle_event` checks against the disk once more.
        This is both the scan at attach (watchdog only reports changes made
        after the observer starts, so files already there, or deleted while
        the app was down, are otherwise never seen) and the periodic one.

        Removal is skipped, never guessed: when the root is missing, when a
        directory could not be listed (its files would look deleted), or when
        the root holds no files at all while the index holds some (an empty
        mount point looks exactly like that). Deletes still arrive as events.

        Updates `watched_folders.last_scan_at` when done, so the API's
        scan_status flips from "scanning" to "idle" and the folder says when
        its files were last compared with the index. Also tallies the
        per-folder ``skipped_count`` + ``skipped_extensions`` from files
        the watcher rejected for an unsupported extension (NOT counting
        intentional-noise filters: dotfiles, AppleDouble, __MACOSX,
        Excalidraw, or a document's JSON metadata sidecar).
        """
        if not os.path.isdir(root):
            logger.warning("watcher: reconcile of %s skipped: the root is missing", root)
            return
        logger.info("watcher: reconcile of %s starting", root)
        count = 0
        skipped_count = 0
        skipped_exts: set[str] = set()
        present: set[str] = set()
        walk_errors: list[OSError] = []
        seen = self._seen_stats.setdefault(folder_id, {})
        active = self._active_paths(folder_id)
        try:
            for dirpath, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
                # Prune dotfile / __MACOSX directories so we don't recurse
                # into them — both for speed and to avoid the cost of
                # invoking _on_event for every file in a .git tree.
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
                for fname in filenames:
                    if self._stop.is_set():
                        # Early stop: nothing is removed and last_scan_at is
                        # not stamped; the next start runs the whole walk.
                        return
                    abs_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
                    present.add(rel_path)

                    # Classify BEFORE handing off to _on_event so we can
                    # tally UNSUPPORTED_EXTENSION skips without going
                    # through the event-handler path twice.
                    # `filenames` is this directory's listing already; passing it spares
                    # a scandir per .json (quadratic in a flat folder of annotated files).
                    reason = classify_skip(rel_path, abs_path, sibling_names=filenames)
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
                    if reason is not None:
                        # NOISE and SIDECAR are intentional; don't tally and
                        # don't dispatch. A sidecar indexed as a document by
                        # an earlier build is retired when the scan reaches
                        # its sibling (handle_event), so it needs no visit.
                        continue

                    stat = _stat_key(abs_path)
                    if stat is not None and rel_path in active and seen.get(rel_path) == stat:
                        continue
                    handled = self._on_event(
                        FileEvent(
                            kind=EventKind.created,
                            folder_id=folder_id,
                            relative_path=rel_path,
                            absolute_path=abs_path,
                        )
                    )
                    if handled and stat is not None:
                        seen[rel_path] = stat
                    count += 1
        except Exception:
            logger.exception("watcher: reconcile of %s failed", root)
            return

        missing = sorted(active - present)
        removed = 0
        if walk_errors:
            logger.warning(
                "watcher: reconcile of %s could not list %d directories (first: %s); nothing removed",
                root,
                len(walk_errors),
                walk_errors[0],
            )
        elif missing and not present:
            logger.warning(
                "watcher: reconcile of %s found no files while %d are indexed; nothing removed "
                "(an empty mount point looks the same)",
                root,
                len(active),
            )
        else:
            for rel_path in missing:
                seen.pop(rel_path, None)
                self._on_event(
                    FileEvent(
                        kind=EventKind.deleted,
                        folder_id=folder_id,
                        relative_path=rel_path,
                        absolute_path=os.path.join(root, rel_path),
                    )
                )
                removed += 1

        # Mark the pass complete and write the skip tally on the folder row.
        # The stamp lands after the walk on purpose: a file present at last_scan_at has had its hash compared
        # to the index. The eval harness relies on that when it decides whether to trust an index it did not
        # build (scripts/test_corpora/runner/sweep.py, _files_the_index_did_not_read, #740).
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
            "watcher: reconcile of %s complete (%d files compared, %d missing removed, %d skipped — extensions=%s)",
            root,
            count,
            removed,
            skipped_count,
            sorted(skipped_exts),
        )

    def _active_paths(self, folder_id: uuid.UUID) -> set[str]:
        sess = self._session_factory()
        try:
            rows = (
                sess.query(WatchedFile.relative_path)
                .filter_by(folder_id=folder_id, status=WatchedFileStatus.active)
                .all()
            )
            return {row.relative_path for row in rows}
        finally:
            sess.close()

    def _monitor_loop(self) -> None:
        """Check every root often (one stat each), and reconcile every folder on the rescan interval."""
        interval = get_settings().watch_rescan_interval_secs
        next_rescan = time.monotonic() + interval
        while not self._stop.is_set():
            self._wake.wait(timeout=ROOT_CHECK_INTERVAL_SECS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._check_roots()
                if interval > 0 and time.monotonic() >= next_rescan:
                    next_rescan = time.monotonic() + interval
                    self._sync_observers()
                    with self._observers_lock:
                        folder_ids = list(self._observers)
                    for fid in folder_ids:
                        self._request_reconcile(fid)
            except Exception:
                logger.exception("watcher: monitor pass failed")

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
            # Guarded on its own: it queries the database too, and an exception
            # here used to end the discovery thread for good (#593).
            try:
                self._sync_observers()
            except Exception:
                logger.exception("watcher: observer sync after discovery failed; retrying next pass")
            self._stop.wait(timeout=DISCOVERY_INTERVAL_SECS)

    def _listener_loop(self) -> None:
        # We don't need the payload itself — any NOTIFY arrival means a folder
        # row changed; _sync_observers() reads the current world from DB.
        streak = FailureStreak()
        listening_since: float | None = None
        announced = False  # the "reconnected" line, once per streak

        def _on_listening() -> None:
            nonlocal listening_since, announced
            listening_since = time.monotonic()
            if streak.failures:
                # NOTIFYs sent while disconnected are lost; resync once LISTEN is
                # back in place, so nothing changed in the gap is missed.
                if not announced:
                    logger.info("watcher: listener reconnected after %d failed attempt(s)", streak.failures)
                    announced = True
                self._sync_observers()

        while not self._stop.is_set():
            try:
                for _ in listen_for_folder_changes(
                    self._stop, poll_interval=NOTIFY_POLL_INTERVAL_SECS, on_listening=_on_listening
                ):
                    self._sync_observers()
            except Exception as exc:
                # The streak ends only once a connection has stayed up a while.
                # Ending it on a successful LISTEN let a connection that accepts
                # LISTEN and drops at the first poll() retry at the base delay
                # forever, with a traceback, a resync and a "reconnected" line
                # every time.
                if listening_since is not None and time.monotonic() - listening_since >= LISTENER_HEALTHY_SECS:
                    streak.reset()
                    announced = False
                listening_since = None
                loudness = streak.record(exc)
                if loudness == "first":
                    logger.exception("watcher: folder-change listener failed; reconnecting with backoff")
                elif loudness is not None:
                    logger.warning(
                        "watcher: folder-change listener still failing (failure %d, %s): %s",
                        streak.failures,
                        "error changed" if loudness == "changed" else "same error",
                        describe_error(exc),
                        exc_info=loudness == "changed",
                    )
                else:
                    logger.debug(
                        "watcher: listener failed again (failure %d): %s", streak.failures, describe_error(exc)
                    )
                delay = min(LISTENER_RETRY_MAX_SECS, LISTENER_RETRY_BASE_SECS * 2 ** min(streak.failures - 1, 8))
                self._stop.wait(timeout=delay)

    def start(self) -> None:
        self._sync_observers()
        for target in (self._discovery_loop, self._listener_loop, self._monitor_loop):
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
        self._wake.set()
        if self._mail_loop is not None and self._mail_observer is not None and self._mail_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._mail_observer.stop(), self._mail_loop).result(timeout=10)
            except Exception:
                logger.exception("watcher: error stopping mail observer")
        if self._mail_thread is not None:
            self._mail_thread.join(timeout=10)
        with self._observers_lock:
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
