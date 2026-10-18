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
file is the document.
"""

from __future__ import annotations

import logging
import os

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

    A relative path that is not already normal (a ``..`` or ``.`` segment, a
    doubled slash) matches nothing.
    """
    if not source_path:
        return None
    for folder_path, relative_path in rows:
        for root in dict.fromkeys((folder_path, os.path.realpath(folder_path))):
            watched = os.path.join(root, relative_path)
            if os.path.normpath(watched) == watched and watched == source_path:
                return watched
    return None


def in_place_path(session, doc) -> str | None:
    """``verified_in_place_path`` for ``doc``, on a sync session.

    The query runs in a savepoint: if it fails, the savepoint is rolled back,
    the failure is logged and the answer is None (the caller reads the document
    as it would any other), and the session's transaction stays usable.
    """
    doc_id, source_path = doc.doc_id, doc.source_path
    try:
        with session.begin_nested():
            rows = session.execute(watched_paths_query(doc_id)).all()
    except Exception as exc:
        logger.warning("watched-file lookup failed for doc %s: %s", doc_id, exc)
        return None
    return verified_in_place_path(rows, source_path)


async def in_place_path_async(session, doc) -> str | None:
    """``verified_in_place_path`` for ``doc``, on an async session. Errors propagate."""
    rows = (await session.execute(watched_paths_query(doc.doc_id))).all()
    return verified_in_place_path(rows, doc.source_path)
