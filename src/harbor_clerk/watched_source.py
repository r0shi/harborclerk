"""Whether a document is read in place from a watched folder, and from where.

One definition, used by every reader of a document's bytes (the extract and OCR
stages, the download route, the metadata re-extract script), by the metadata
extractors (only this path reaches them, so a sidecar is looked for beside it
and nowhere else) and by the upload confirm (which refuses a new version of
such a document):

A document is read in place when it has an active watched file and its
``source_path`` is exactly that file's path, computed from the watched file (its
folder's path, or that path's realpath, joined with its relative path), never
taken from the document. The folder's enabled state does not matter: a paused
folder's documents are still its files. A ``source_path`` set any other way is
a label a client supplied, not a file this process may read or look beside.

A document read in place is read from that path even when it also carries a
stored original (a new version confirmed before that was refused, #859): the
file is the document. If that file is missing (mid atomic save, or gone), the
read fails rather than falling back to the stored original, which may be an
older upload; the watcher's next event re-queues the document or retires it. A
document not read in place is read from its stored original; with none, there
is nothing to read, and the read fails rather than guess at ``source_path``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import Select, select

logger = logging.getLogger(__name__)


def watched_paths_query(doc_id) -> Select:
    """``(folder path, relative path)`` of each active watched file mapped to the
    document. Usable with a sync or an async session; feed the rows to
    ``verified_in_place_path``."""
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

    return (
        select(WatchedFolder.path, WatchedFile.relative_path)
        .join(WatchedFolder, WatchedFolder.folder_id == WatchedFile.folder_id)
        .where(WatchedFile.doc_id == doc_id, WatchedFile.status == WatchedFileStatus.active)
    )


def verified_in_place_path(rows, source_path: str | None) -> str | None:
    """The watched path, computed from ``rows``, that ``source_path`` is exactly; or None.

    A relative path that is absolute, or not already normal (a ``..`` or ``.``
    segment, a doubled slash), matches nothing.
    """
    if not source_path:
        return None
    rows = list(rows)
    # The plain folder path first: it is how the watcher builds source_path, and it
    # touches no disk. The realpath (which can block on a stalled network mount)
    # only on a miss.
    for folder_path, relative_path in rows:
        watched = _normal_join(folder_path, relative_path)
        if watched is not None and watched == source_path:
            return watched
    for folder_path, relative_path in rows:
        real = os.path.realpath(folder_path)
        if real == folder_path:
            continue
        watched = _normal_join(real, relative_path)
        if watched is not None and watched == source_path:
            return watched
    return None


def _normal_join(root: str, relative_path: str) -> str | None:
    """``root`` joined with ``relative_path``, or None when the relative path is not
    plain: absolute (``os.path.join`` would discard ``root``), with a ``..`` component,
    or not already normal."""
    if not relative_path or os.path.isabs(relative_path) or ".." in relative_path.split("/"):
        return None
    joined = os.path.join(root, relative_path)
    return joined if os.path.normpath(joined) == joined else None


def canonical_source_path(folder_path: str, relative_path: str) -> str | None:
    """The ``source_path`` a watched file is stored under: its folder's path joined
    with its relative path; None when the relative path is not plain."""
    return _normal_join(folder_path, relative_path)


def repaired_source_path(current: str | None, folder_path: str, relative_path: str) -> str | None:
    """The canonical ``source_path`` to store in place of ``current``, or None to leave it.

    Self-repair for a watched document whose stored ``source_path`` is an alias of
    its watched file's path (a symlinked directory, ``/var`` vs ``/private/var``,
    another Unicode normalization), as older clients stored whatever they sent:
    such a document does not verify as read in place, so it could not be read.
    Returns the canonical path when ``current`` differs from it and both name the
    same file on disk. Returns None when it is already canonical, when the
    relative path is not plain, or when the two do not name the same file (logged;
    the row is left alone).
    """
    canonical = canonical_source_path(folder_path, relative_path)
    if canonical is None or current == canonical:
        return None
    if current:
        try:
            same = os.path.realpath(current) == os.path.realpath(canonical) or os.path.samefile(current, canonical)
        except OSError:
            same = False
        if same:
            return canonical
    logger.warning("watched file %s: the document's source_path does not name it; left unchanged", relative_path)
    return None


class SourceUnavailable(RuntimeError):
    """A document's bytes cannot be read; the message says why (and names no path)."""


class WatchedFileMissing(SourceUnavailable):
    """The document is read in place, but its file is missing right now."""


class NoReadableSource(SourceUnavailable):
    """The document is not read in place and has no stored original."""


def in_place_path(session, doc) -> str | None:
    """``verified_in_place_path`` for ``doc``, on a sync session.

    None means the lookup succeeded and the document is not read in place. A
    failed lookup raises: the caller (a pipeline stage) must fail and be retried,
    never fall back to a stored original, which for a document that also carries
    one would index the wrong bytes.
    """
    rows = session.execute(watched_paths_query(doc.doc_id)).all()
    return verified_in_place_path(rows, doc.source_path)


async def in_place_path_async(session, doc) -> str | None:
    """``verified_in_place_path`` for ``doc``, on an async session. Errors propagate.

    The comparison runs in a worker thread: on a miss it calls ``realpath``,
    which must not block the event loop on a stalled mount.
    """
    rows = (await session.execute(watched_paths_query(doc.doc_id))).all()
    return await asyncio.to_thread(verified_in_place_path, rows, doc.source_path)


def read_document_bytes(session, doc) -> tuple[bytes, str | None]:
    """A document's bytes, and the in-place path they came from (None: the stored original).

    Raises ``WatchedFileMissing`` when the document is read in place and its file is
    missing, ``NoReadableSource`` when it is neither read in place nor stored, and
    whatever the watched-file lookup raised when it failed. Never reads an
    unverified ``source_path``, and never substitutes the stored original for a
    file that is read in place.
    """
    in_place = in_place_path(session, doc)
    if in_place is not None:
        try:
            return Path(in_place).read_bytes(), in_place
        except FileNotFoundError:
            raise WatchedFileMissing(
                f"the watched file of doc {doc.doc_id} is missing; it is read again when the file reappears"
            ) from None
    if doc.original_object_key:
        from harbor_clerk.storage import get_storage

        return get_storage().get_object(doc.original_bucket, doc.original_object_key).read(), None
    raise NoReadableSource(
        f"doc {doc.doc_id} has no stored original and its source_path does not match an active watched file"
    )


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
            path = verified_in_place_path(rows, source_path)
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
