"""Whether a document is watched, read in place, and from where.

One definition, used by every reader of a document's bytes (the extract and OCR
stages, the download route, the metadata re-extract script), by the metadata
extractors (only the in-place path reaches them, so a sidecar is looked for beside
it and nowhere else) and by the upload confirm (which refuses a new version of a
watched document):

A document is **watched** when it has a watched file, active or removed. Only
watching creates one, always together with a new document, so an upload never
has one. A watched document is **read in place** when its watched file is active
and its ``source_path`` is exactly that file's path, computed from the watched
file (its folder's path, that path normalized, or its realpath, joined with its
relative path), never taken from the document. The folder's enabled state does
not matter to reading: a paused folder's documents are still its files. A paused
folder is not scanned, though, so its documents are not repaired or re-queued
(below) until it is enabled again. A ``source_path`` set any other way is a label a
client supplied, not a file to read or look beside.

A watched document's bytes are its file's, and nothing else's, even when it also
carries a stored original (a new version confirmed before that was refused,
#859). When the file cannot be read in place (removed, missing mid atomic save,
or a ``source_path`` not yet repaired), the read fails as missing, never falls
back to the stored original, which may be an older upload; the watcher's next
event for that file re-queues the document (or retires it). A document that is
not watched is read from its stored original; with none there is nothing to
read, and the read fails rather than guess at ``source_path``.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Select, select

logger = logging.getLogger(__name__)


def watched_paths_query(doc_id) -> Select:
    """``(folder path, relative path, status)`` of each watched file mapped to the
    document, active or removed. Usable with a sync or an async session; feed the
    rows to ``watched_state_from_rows``."""
    from harbor_clerk.models.watched import WatchedFile, WatchedFolder

    return (
        select(WatchedFolder.path, WatchedFile.relative_path, WatchedFile.status)
        .join(WatchedFolder, WatchedFolder.folder_id == WatchedFile.folder_id)
        .where(WatchedFile.doc_id == doc_id)
    )


@dataclass(frozen=True)
class WatchedState:
    """``watched``: the document has a watched file, active or removed.
    ``path``: the verified in-place path, when it is read in place (else None)."""

    watched: bool
    path: str | None


def watched_state_from_rows(rows, source_path: str | None) -> WatchedState:
    """The ``WatchedState`` that ``watched_paths_query``'s rows give for ``source_path``."""
    from harbor_clerk.models.watched import WatchedFileStatus

    rows = list(rows)
    active = [(folder, relative) for folder, relative, status in rows if status == WatchedFileStatus.active]
    return WatchedState(watched=bool(rows), path=verified_in_place_path(active, source_path))


def verified_in_place_path(rows, source_path: str | None) -> str | None:
    """The watched path, computed from ``rows``, that ``source_path`` is exactly; or None.

    A relative path that is absolute, or not already normal (a ``..`` or ``.``
    segment, a doubled slash), matches nothing.
    """
    if not source_path:
        return None
    rows = list(rows)
    # The folder path as stored first: it is how the watcher builds source_path
    # (os.walk and relpath from that path, normalized or not), and it touches no
    # disk. Then the folder path normalized (no disk either). The realpath, which
    # can block on a stalled network mount, only on a miss.
    for folder_path, relative_path in rows:
        watched = _normal_join(folder_path, relative_path)
        if watched is not None and watched == source_path:
            return watched
    for resolve in (os.path.normpath, os.path.realpath):
        for folder_path, relative_path in rows:
            root = resolve(folder_path)
            if root == folder_path:
                continue
            watched = _normal_join(root, relative_path)
            if watched is not None and watched == source_path:
                return watched
    return None


def _normal_join(root: str, relative_path: str) -> str | None:
    """``root`` joined with ``relative_path``, or None when the relative path is not
    plain: empty, absolute (``os.path.join`` would discard ``root``), with a ``..``
    component, or not already normal (a ``.`` segment, a doubled or trailing slash).
    The folder side is taken as given: the watcher joins onto it unnormalized."""
    if (
        not relative_path
        or os.path.isabs(relative_path)
        or ".." in relative_path.split("/")
        or os.path.normpath(relative_path) != relative_path
    ):
        return None
    return os.path.join(root, relative_path)


def canonical_source_path(folder_path: str, relative_path: str) -> str | None:
    """The ``source_path`` a watched file is stored under: its folder's path joined
    with its relative path; None when the relative path is not plain."""
    return _normal_join(folder_path, relative_path)


def repaired_source_path(current: str | None, folder_path: str, relative_path: str) -> str | None:
    """The canonical ``source_path`` to store in place of ``current``, or None to leave it.

    The active watched file (folder, relative path) is authoritative and
    ``source_path`` is a label. Self-repair for a watched document whose stored
    label is not its watched file's canonical path, whatever it is: an alias (a
    symlinked directory, ``/var`` vs ``/private/var``, another Unicode
    normalization), as older clients stored whatever they sent, or a client's
    label (None, a relative name, some other file) from a new version confirmed
    before that was refused (#859). Such a document does not verify as read in
    place, so it could not be read.

    Returns the canonical path when ``current`` differs from it and the canonical
    path is a regular file under exactly that name, inside the folder (its
    realpath under the folder's). Returns None when it is already canonical, or
    when the canonical path is not plain, not inside the folder, or not present.
    """
    canonical = canonical_source_path(folder_path, relative_path)
    if canonical is None or current == canonical:
        return None
    try:
        real_folder = os.path.realpath(folder_path)
        inside = os.path.realpath(canonical).startswith(real_folder + os.sep)
    except (OSError, ValueError):
        inside = False
    if not inside or not is_file_exact_case(canonical, folder_path):
        logger.warning("watched file %s is not present inside its folder; source_path left unchanged", relative_path)
        return None
    try:
        alias = bool(current) and (
            os.path.realpath(current) == os.path.realpath(canonical) or os.path.samefile(current, canonical)
        )
    except (OSError, ValueError):
        alias = False
    if not alias:
        logger.info(
            "watched file %s: replacing a source_path that did not name it with its canonical path", relative_path
        )
    return canonical


class SourceUnavailable(RuntimeError):
    """A document's bytes cannot be read; the message says why (and names no path)."""


class WatchedFileMissing(SourceUnavailable):
    """The document is watched, but its file cannot be read in place right now."""


class NoReadableSource(SourceUnavailable):
    """The document is not watched and has no stored original."""


_MISSING_SOURCE_ERRORS = frozenset(cls.__name__ for cls in (WatchedFileMissing, NoReadableSource))

# What the extract and OCR stages raised for an unreadable source before
# read_document_bytes existed ("No source for doc <id>: ..." and "No source for doc
# <id>"), as describe_error rendered it: documents that errored that way before an
# upgrade recover like the ones above.
_LEGACY_MISSING_SOURCE_PREFIX = "RuntimeError: No source for doc "


class _Listing:
    """One directory's entry names, with an NFC index built on first need, and its
    regular files (``DirEntry.is_file``, symlinks followed) when listed with them."""

    __slots__ = ("names", "files", "_nfc")

    def __init__(self, names: list[str], files: tuple[str, ...] | None = None):
        self.names = frozenset(names)
        self.files = files
        self._nfc: dict[str, str] | None = None

    def find(self, name: str) -> str | None:
        """The entry that is ``name`` exactly, else the one equal to it NFC-normalised; None when there is none."""
        if name in self.names:
            return name
        if self._nfc is None:
            self._nfc = {unicodedata.normalize("NFC", n): n for n in self.names}
        return self._nfc.get(unicodedata.normalize("NFC", name))


# Listings reused across checks, so a burst of moves into one large directory
# lists it once rather than once per file (`mv proj proj2023` with 8,000 files
# spent 17 s listing, a 20,000-file case-only rename 110 s, serially under the
# folder lock). An entry is used only while its directory's stat is unchanged,
# and stored only when the directory had not changed for a settle time before
# it was listed: timestamps are coarse (a kernel tick on Linux, 1 s on HFS+, 2 s
# on FAT), so a change in the same tick as the last one leaves the stat as it
# was. A volume stamping whole seconds waits LISTING_SETTLED_SECS, any other
# LISTING_SETTLED_FINE_SECS. A name the cache lacks is looked up in a fresh
# listing before the answer is "absent", since that answer retires a document.
LISTING_SETTLED_SECS = 2.0
LISTING_SETTLED_FINE_SECS = 0.05
LISTING_CACHE_DIRS = 64
_listings: OrderedDict[str, tuple[tuple, _Listing]] = OrderedDict()
_listings_lock = threading.Lock()


def _dir_key(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_nlink)


def _settle_secs(st: os.stat_result) -> float:
    whole_seconds = st.st_mtime_ns % 1_000_000_000 == 0 or st.st_ctime_ns % 1_000_000_000 == 0
    return LISTING_SETTLED_SECS if whole_seconds else LISTING_SETTLED_FINE_SECS


def _listing(directory: str, *, fresh: bool = False, files: bool = False) -> _Listing:
    """``directory``'s entries, from the cache when its stat is unchanged (unless ``fresh``);
    with ``files``, its regular files too (one ``scandir``). Raises OSError."""
    st = os.stat(directory)  # before the listing: a change after it gives a new key
    key = _dir_key(st)
    if not fresh:
        with _listings_lock:
            hit = _listings.get(directory)
            if hit is not None and hit[0] == key and (not files or hit[1].files is not None):
                _listings.move_to_end(directory)
                return hit[1]
    if files:
        with os.scandir(directory) as it:
            entries = list(it)
        listing = _Listing([e.name for e in entries], tuple(e.name for e in entries if e.is_file()))
    else:
        listing = _Listing(os.listdir(directory))
    if time.time() - max(st.st_mtime_ns, st.st_ctime_ns) / 1e9 >= _settle_secs(st):
        with _listings_lock:
            _listings[directory] = (key, listing)
            _listings.move_to_end(directory)
            while len(_listings) > LISTING_CACHE_DIRS:
                _listings.popitem(last=False)
    return listing


def entry_named(directory: str, name: str) -> str | None:
    """The entry of ``directory`` spelled ``name`` (exactly, else NFC-equal), or None when there is none.

    Answered from the shared listing cache; an absent name is confirmed in a
    fresh listing. Raises OSError when the directory cannot be listed.
    """
    found = _listing(directory).find(name)
    return found if found is not None else _listing(directory, fresh=True).find(name)


def entry_names(directory: str, *, fresh: bool = False) -> frozenset[str]:
    """Every entry name in ``directory``, from the shared listing cache unless ``fresh``. Raises OSError."""
    return _listing(directory, fresh=fresh).names


def file_names(directory: str) -> tuple[str, ...]:
    """The regular files in ``directory`` (symlinks followed), from the shared listing cache. Raises OSError."""
    files = _listing(directory, files=True).files
    assert files is not None
    return files


def is_file_exact_case(path: str, root: str | None = None) -> bool:
    """True when ``path`` is a regular file (a symlink to one counts) under exactly
    that spelling. A dangling symlink or a directory is not. The one presence check
    for a watched file: the watcher's delete handling, the source_path repair and
    the /watch routes all use it.

    ``os.path.isfile`` alone answers yes for ``Invoices/Doc.pdf`` after a rename
    to ``invoices/doc.pdf`` on a case-insensitive volume (APFS's default): it
    resolves each component case-insensitively, which would keep a renamed-away
    path alive beside the new one. So the name is looked up in its parent's
    listing, and with ``root`` (the watched root, whose own spelling is the
    folder's) every component of ``path`` below it is. Names are compared
    NFC-normalised, since macOS may hand back either form, and the walk continues
    under the name the listing holds. A ``path`` not under ``root`` has its leaf
    checked only.

    ``isfile`` goes first: a path that does not resolve to a file is answered
    without listing anything, since listing a 20,000-entry directory per deleted
    file made ``rm *.pdf`` cost minutes of serial work under the folder lock. A
    path that does resolve reuses its directories' listings while they are
    unchanged (``_listing``), so checks in one directory list it once.

    A file under a directory that cannot be listed (search permission without
    read, mode 0311) cannot be settled; it counts as present, since guessing a
    deletion would retire a live document.
    """
    if not os.path.isfile(path):
        return False
    prefix = root if root is None or root.endswith(os.sep) else root + os.sep
    if prefix is not None and path.startswith(prefix) and len(path) > len(prefix):
        current, parts = root, path[len(prefix) :].split(os.sep)
    else:
        current, parts = os.path.dirname(path) or ".", [os.path.basename(path)]
    for part in parts:
        try:
            name = entry_named(current, part)
        except OSError:
            return True
        if name is None:
            return False
        current = os.path.join(current, name)
    return os.path.isfile(current)


def failed_for_a_missing_source(doc) -> bool:
    """True when ``doc``'s last run errored because its source could not be read
    (``WatchedFileMissing`` or ``NoReadableSource``), so seeing its file again is
    reason to queue it again. Any other error (a corrupt file, a Tika failure) is
    not retried this way.

    Keyed on the error the worker records: ``worker/entry.py`` writes
    ``error_text.describe_error(exc)``, ``"TypeName: message"``, into
    ``documents.error``. The pre-upgrade form ("RuntimeError: No source for doc
    ...") matches too, and nothing broader.
    """
    from harbor_clerk.models.enums import PipelineStatus

    if doc.pipeline_status != PipelineStatus.error or not doc.error:
        return False
    if doc.error.startswith(_LEGACY_MISSING_SOURCE_PREFIX):
        return True
    return doc.error.split(":", 1)[0].strip() in _MISSING_SOURCE_ERRORS


def watched_state(session, doc) -> WatchedState:
    """The ``WatchedState`` of ``doc``, on a sync session.

    A failed lookup (a database error) raises. In a pipeline stage that fails the
    stage: the job and document are marked as errors with that error. There is no
    automatic retry, and the watcher does not re-queue it (it re-queues only
    missing-source failures and stale indexes); it is run again by a reprocess, or
    when the file's bytes change. It never falls back to a stored original, which
    for a document that also carries one would index the wrong bytes.
    """
    rows = session.execute(watched_paths_query(doc.doc_id)).all()
    return watched_state_from_rows(rows, doc.source_path)


async def watched_state_async(session, doc) -> WatchedState:
    """The ``WatchedState`` of ``doc``, on an async session. Errors propagate.

    The comparison runs in a worker thread: on a miss it calls ``realpath``,
    which must not block the event loop on a stalled mount.
    """
    rows = (await session.execute(watched_paths_query(doc.doc_id))).all()
    return await asyncio.to_thread(watched_state_from_rows, rows, doc.source_path)


def errno_name(exc: OSError) -> str:
    """``exc``'s errno as its symbolic name ("EACCES"), else the exception's class name."""
    return errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__


def log_read_error(exc: OSError, doc_id, path: str) -> str:
    """Log a failed read of a watched file (the path in the log only) and return the
    errno name for an error message that must not carry the path.

    A file that is simply gone (ENOENT, often mid atomic save) logs at INFO; anything
    else (EACCES, EIO, ESTALE, ETIMEDOUT...) at WARNING.
    """
    name = errno_name(exc)
    level = logging.INFO if exc.errno == errno.ENOENT else logging.WARNING
    logger.log(level, "reading the watched file of doc %s failed (%s): %s", doc_id, name, path)
    return name


def read_document_bytes(session, doc) -> tuple[bytes, str | None]:
    """A document's bytes, and the in-place path they came from (None: the stored original).

    Raises ``WatchedFileMissing`` when the document is watched and its file cannot
    be read in place (removed, missing, or a ``source_path`` that does not verify),
    ``NoReadableSource`` when it is not watched and has no stored original, and
    whatever the watched-file lookup raised when it failed. Never reads an
    unverified ``source_path``, and never reads a watched document's stored original.
    """
    state = watched_state(session, doc)
    reason = "removed, missing, or moved"
    if state.path is not None:
        try:
            return Path(state.path).read_bytes(), state.path
        except OSError as exc:
            # Missing, a path component no longer a directory, a directory in its
            # place, a symlink loop, no permission, an I/O or network-mount error:
            # all "cannot be read in place". The path goes to the log only; the
            # error raised below (which lands in documents.error) names the errno.
            reason = log_read_error(exc, doc.doc_id, state.path)
    if state.watched:
        raise WatchedFileMissing(
            f"the watched file of doc {doc.doc_id} cannot be read in place ({reason}); "
            "the watcher's next event for the file queues it again"
        )
    if doc.original_object_key:
        from harbor_clerk.storage import get_storage

        return get_storage().get_object(doc.original_bucket, doc.original_object_key).read(), None
    raise NoReadableSource(f"doc {doc.doc_id} is not a watched document and has no stored original")


def display_filename(session, doc) -> str:
    """The document's filename for job events and logs: the watched file's when it
    is read in place, else the stored original's, else ``source_path``'s, else "".

    Display only: the watched-file lookup runs only for a document carrying both a
    stored original and a ``source_path`` (the one shape where the answers can
    differ) and a session is given, in a savepoint, and a failure falls back silently.
    """
    doc_id, object_key, source_path = doc.doc_id, doc.original_object_key, doc.source_path
    if session is not None and object_key and source_path:
        try:
            with session.begin_nested():
                rows = session.execute(watched_paths_query(doc_id)).all()
            path = watched_state_from_rows(rows, source_path).path
        except Exception:
            logger.debug("watched-file lookup for a display name failed for doc %s", doc_id, exc_info=True)
            path = None
        if path:
            return os.path.basename(path)
    if object_key:
        return os.path.basename(object_key)
    if source_path:
        return os.path.basename(source_path)
    return ""
