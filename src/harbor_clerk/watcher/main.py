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
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy import case, null, update
from sqlalchemy.orm import sessionmaker

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_engine
from harbor_clerk.error_text import describe_error
from harbor_clerk.log_setup import setup_logging
from harbor_clerk.models.watched import (
    ROOT_EMPTY,
    ROOT_MISSING,
    ROOT_UNREADABLE,
    WATCHER_UNAVAILABLE_REASONS,
    WatchedFile,
    WatchedFileStatus,
    WatchedFolder,
)
from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.watcher.events import (
    EventKind,
    FileEvent,
    SkipReason,
    classify_skip,
    handle_event,
    sha256_of,
)
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
# A failing re-attach is retried with backoff, at least once every this many checks plus one (~5 min).
REATTACH_MAX_SKIPPED_CHECKS = 59

StatKey = tuple[int, int, int, int]


def _fold(relative_path: str) -> str:
    """A path as a case-insensitive, normalisation-insensitive volume compares it."""
    return unicodedata.normalize("NFC", relative_path).casefold()


def _unlisted_dirs(root: str, errors: list[OSError]) -> list[str] | None:
    """Relative paths of the directories ``os.walk`` could not list; None when the root itself is one."""
    unlisted = []
    for err in errors:
        rel = os.path.relpath(err.filename or root, root).replace(os.sep, "/")
        if rel == ".":
            return None
        unlisted.append(rel)
    return unlisted


def _listable(path: str) -> bool:
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False


def _under(relative_path: str, directories: list[str]) -> bool:
    return any(relative_path.startswith(d + "/") for d in directories)


def _stat_key(path: str) -> StatKey | None:
    """What a reconciliation compares to call a file unchanged.

    Not ctime: evicting an iCloud file to a dataless placeholder (or
    downloading it again) changes its ctime and nothing else, so a ctime key
    would re-hash, and so re-download, every evicted file on every pass. A
    rewrite that keeps size, mtime and inode is missed only by this backstop;
    the live observer reports it, and the next start hashes everything.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


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
        # The watcher reason each folder's last pass recorded (`_record_pass`), so `_check_roots` can
        # notice a root that is back without waiting for the periodic pass.
        self._reasons: dict[uuid.UUID, str] = {}
        # Per (folder, path) whose events keep failing: one traceback per streak.
        self._event_failures: dict[tuple[uuid.UUID, str], FailureStreak] = {}
        # Per folder whose observer failed to attach: [consecutive failures, root checks still to skip].
        self._reattach_failures: dict[uuid.UUID, list[int]] = {}
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
        """Apply one event in its own transaction. Returns False when it failed (logged).

        A file that fails the same way on every pass (unreadable: mode 000, or
        TCC-protected) logs its traceback once per failure streak, then at
        debug, with #593's FailureStreak; a success ends the streak.
        """
        key = (event.folder_id, event.relative_path)
        with self._folder_lock(event.folder_id):
            sess = self._session_factory()
            try:
                handle_event(sess, event)
                sess.commit()
            except Exception as exc:
                sess.rollback()
                streak = self._event_failures.setdefault(key, FailureStreak())
                loudness = streak.record(exc)
                if loudness == "first":
                    logger.exception("watcher: failed to handle event %r", event)
                elif loudness is not None:
                    logger.warning(
                        "watcher: event for %s still failing (failure %d, %s): %s",
                        event.relative_path,
                        streak.failures,
                        "error changed" if loudness == "changed" else "same error",
                        describe_error(exc),
                        exc_info=loudness == "changed",
                    )
                else:
                    logger.debug(
                        "watcher: event for %s failed again (failure %d): %s",
                        event.relative_path,
                        streak.failures,
                        describe_error(exc),
                    )
                return False
            finally:
                sess.close()
        streak = self._event_failures.pop(key, None)
        if streak is not None:
            logger.info("watcher: %s handled after %d failed attempt(s)", event.relative_path, streak.failures)
        if self._reasons.get(event.folder_id) == ROOT_EMPTY and event.kind != EventKind.deleted:
            self._clear_root_empty_if_indexed(event)
        return True

    def _clear_root_empty_if_indexed(self, event: FileEvent) -> None:
        """A live event that indexed a file ends `root_empty` now, not at the next periodic pass."""
        rel = event.dest_relative_path or event.relative_path
        sess = self._session_factory()
        try:
            indexed = (
                sess.query(WatchedFile.file_id)
                .filter_by(folder_id=event.folder_id, relative_path=rel, status=WatchedFileStatus.active)
                .first()
                is not None
            )
            if not indexed:
                return
            sess.execute(
                update(WatchedFolder)
                .where(WatchedFolder.folder_id == event.folder_id, WatchedFolder.unavailable_reason == ROOT_EMPTY)
                .values(unavailable_reason=None)
            )
            sess.commit()
            self._reasons.pop(event.folder_id, None)
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to clear root_empty for %s", event.folder_id)
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
                    self._reattach_failures.pop(fid, None)
                    self._reasons.pop(fid, None)
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
                    # Still run the pass below: it stamps last_scan_at and records
                    # why (root missing or unreadable), so the folder does not read
                    # "scanning" for good. `_check_roots` retries the attach.
                    logger.exception("watcher: failed to start observer for %s (%s)", fid, path)
                    self._reattach_failures[fid] = [1, 0]
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
                    # The pass finds the root gone and records why, removing nothing.
                    self._request_reconcile(fid)
                continue
            self._missing_roots.discard(fid)
            if state is RootState.ok:
                # A root that was missing or unreadable and is back (inotify keeps
                # watching a root moved aside and back) is compared now, which
                # clears the reason, rather than at the next periodic pass or never.
                if self._reasons.get(fid) in (ROOT_MISSING, ROOT_UNREADABLE) and _listable(obs.root):
                    self._request_reconcile(fid)
                continue
            backoff = self._reattach_failures.get(fid)
            if backoff is not None and backoff[1] > 0:
                backoff[1] -= 1  # backing off after repeated failures
                continue
            with self._observers_lock:
                if self._observers.get(fid) is not obs:
                    continue
                logger.info("watcher: re-attaching observer for %s", obs.root)
                try:
                    obs.restart()
                except Exception:
                    failures = backoff[0] + 1 if backoff is not None else 1
                    # Retry after 0, 1, 3, 7 ... checks, at most every REATTACH_MAX_SKIPPED_CHECKS + 1.
                    self._reattach_failures[fid] = [
                        failures,
                        min(2 ** (failures - 1), REATTACH_MAX_SKIPPED_CHECKS + 1) - 1,
                    ]
                    if backoff is None:
                        logger.exception(
                            "watcher: failed to re-attach observer for %s; retrying with backoff", obs.root
                        )
                        # A pass still runs, so the folder records why (root unreadable) and
                        # its last_scan_at, instead of reading "scanning" while this retries.
                        self._request_reconcile(fid)
                    else:
                        logger.debug("watcher: re-attach of %s failed again (%d attempts)", obs.root, failures)
                    continue
                if backoff is not None:
                    logger.info("watcher: re-attached observer for %s after %d failed attempts", obs.root, backoff[0])
                self._reattach_failures.pop(fid, None)
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
        active row. A new path that differs from a vanished active one only by
        case or Unicode normalisation (``mv Invoices invoices`` while the app
        was down) is dispatched as a move, so the file keeps its document.
        Then every active row whose file is gone is dispatched as a delete,
        which `handle_event` checks against the disk once more. This is both
        the scan at attach (watchdog only reports changes made after the
        observer starts, so files already there, or deleted while the app was
        down, are otherwise never seen) and the periodic one.

        Removal is skipped, never guessed. Under a directory that could not be
        listed (permissions, a TCC-protected folder), its files would look
        deleted, so nothing under it is removed; the rest of the folder is
        reconciled as usual. The whole folder is skipped when the root is
        missing or cannot be listed, or holds no files at all while the index
        holds some (an empty mount point looks exactly like that). Deletes
        still arrive as events.

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
            self._record_pass(folder_id, unavailable=ROOT_MISSING)
            return
        logger.info("watcher: reconcile of %s starting", root)
        count = 0
        skipped_count = 0
        skipped_exts: set[str] = set()
        present: set[str] = set()
        # Files the watcher would index. Noise (.DS_Store, Icon\r, AppleDouble) and unsupported files do not
        # make a root non-empty: Finder writes a .DS_Store into any folder it shows, mount point or not.
        admitted = 0
        walk_errors: list[OSError] = []
        seen = self._seen_stats.setdefault(folder_id, {})
        active = self._active_paths(folder_id)
        active_by_fold: dict[str, list[str]] = {}
        for path in sorted(active):
            active_by_fold.setdefault(_fold(path), []).append(path)
        respelled: list[tuple[str, str]] = []
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
                    if reason is SkipReason.SIDECAR and rel_path in active:
                        # A sidecar still indexed as a document (by an earlier
                        # build, or a missed event before its sibling landed) is
                        # retired here, by its own visit: the sibling's visit
                        # also retires it, but an unchanged sibling is skipped
                        # by the stat cache.
                        self._on_event(FileEvent(EventKind.created, folder_id, rel_path, abs_path))
                        continue
                    if reason is not None:
                        # NOISE and SIDECAR are intentional; don't tally and
                        # don't dispatch.
                        continue

                    admitted += 1
                    stat = _stat_key(abs_path)
                    if stat is not None and rel_path in active and seen.get(rel_path) == stat:
                        continue
                    if rel_path not in active and any(p != rel_path for p in active_by_fold.get(_fold(rel_path), [])):
                        # Decided after the walk, once it is known whether the
                        # other spelling is still on disk too.
                        respelled.append((rel_path, abs_path))
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

        unlisted = _unlisted_dirs(root, walk_errors)
        if unlisted is None:
            logger.warning("watcher: reconcile of %s skipped: the root could not be listed (%s)", root, walk_errors[0])
            self._record_pass(folder_id, unavailable=ROOT_UNREADABLE)
            return
        if unlisted:
            logger.warning(
                "watcher: reconcile of %s could not list %d directories (first: %s); nothing under them removed",
                root,
                len(unlisted),
                walk_errors[0] if walk_errors else unlisted[0],
            )

        moved_from: set[str] = set()
        for rel_path, abs_path in respelled:
            old = next(
                (
                    p
                    for p in active_by_fold.get(_fold(rel_path), [])
                    if p not in present
                    and p not in moved_from
                    and not _under(p, unlisted)
                    and self._same_file_or_content(folder_id, root, p, abs_path)
                ),
                None,
            )
            if old is None:
                event = FileEvent(EventKind.created, folder_id, rel_path, abs_path)
            else:
                moved_from.add(old)
                event = FileEvent(
                    EventKind.moved,
                    folder_id,
                    old,
                    os.path.join(root, old),
                    dest_relative_path=rel_path,
                    dest_absolute_path=abs_path,
                )
            self._on_event(event)
            count += 1

        missing = sorted(p for p in active - present - moved_from if not _under(p, unlisted))
        removed = 0
        empty = bool(missing) and admitted == 0
        if empty:
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

        self._record_pass(
            folder_id,
            unavailable=ROOT_EMPTY if empty else None,
            skipped_count=skipped_count,
            skipped_exts=skipped_exts,
        )

        logger.info(
            "watcher: reconcile of %s complete (%d files compared, %d missing removed, %d skipped — extensions=%s)",
            root,
            count,
            removed,
            skipped_count,
            sorted(skipped_exts),
        )

    def _record_pass(
        self,
        folder_id: uuid.UUID,
        *,
        unavailable: str | None,
        skipped_count: int | None = None,
        skipped_exts: set[str] | None = None,
    ) -> None:
        """Stamp `last_scan_at` and say whether the folder could be compared.

        A completed pass lands after the walk on purpose: a file present at last_scan_at has had its hash
        compared to the index. The eval harness relies on that when it decides whether to trust an index it
        did not build (scripts/test_corpora/runner/sweep.py, _files_the_index_did_not_read, #740).

        A pass skipped because the root is missing or cannot be listed is stamped too, or the API's
        scan_status (`last_scan_at is None`) would read "scanning" for good; `unavailable_reason` says why
        (as it does for an empty root, whose removals were skipped), which the folder list shows, and the
        harness distrusts that stamp. These reasons are informational (`folder_is_unavailable`). The watcher
        sets and clears only its own: "unmounted" belongs to Docker discovery, which re-enables a folder only
        while it reads "unmounted", so it is never overwritten here.
        """
        reason = WatchedFolder.unavailable_reason
        ours = reason.in_(WATCHER_UNAVAILABLE_REASONS)
        if unavailable is None:
            new_reason = case((ours, null()), else_=reason)
        else:
            # Set only over nothing or our own, and never on a disabled folder.
            new_reason = case(((reason.is_(None) | ours) & WatchedFolder.enabled, unavailable), else_=reason)
        values: dict = {"last_scan_at": datetime.now(UTC), "unavailable_reason": new_reason}
        if skipped_count is not None:
            values["skipped_count"] = skipped_count
            values["skipped_extensions"] = sorted(skipped_exts or ())
        sess = self._session_factory()
        try:
            # One statement: a read-then-write would let discovery's "unmounted",
            # committed in between, be overwritten.
            stored = sess.execute(
                update(WatchedFolder)
                .where(WatchedFolder.folder_id == folder_id)
                .values(**values)
                .returning(WatchedFolder.unavailable_reason)
            ).scalar_one_or_none()
            sess.commit()
            if stored in WATCHER_UNAVAILABLE_REASONS:
                self._reasons[folder_id] = stored
            else:
                self._reasons.pop(folder_id, None)
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to update last_scan_at for %s", folder_id)
        finally:
            sess.close()

    def _same_file_or_content(self, folder_id: uuid.UUID, root: str, old: str, new_abs: str) -> bool:
        """Whether a re-spelled path may inherit the vanished one's document.

        On a case-insensitive volume the old spelling still resolves to the new
        file. On a case-sensitive one (Docker's ext4) it is another file that
        merely differs by case, and inherits only if its content is the same.
        """
        try:
            if os.path.samefile(os.path.join(root, old), new_abs):
                return True
        except OSError:
            pass
        sess = self._session_factory()
        try:
            row_sha = sess.query(WatchedFile.sha256).filter_by(folder_id=folder_id, relative_path=old).scalar()
        finally:
            sess.close()
        if row_sha is None:
            return False
        try:
            return sha256_of(new_abs) == row_sha
        except OSError:
            return False

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
