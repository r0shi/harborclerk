"""Filesystem-event → database-write translation.

No watchdog imports, no I/O scheduling. Caller passes in synthetic FileEvent
records; this module does the database work in the provided session. Caller
is responsible for commit. It does read the filesystem: the event's file is
hashed, an `.eml` is parsed, and a `.json` is judged a metadata sidecar or a
document by listing its directory and reading it (`sidecar_owner_names`), and
a delete is checked against the disk before it is believed (an atomic save
reports one for a file that exists). Nothing under a watched folder is ever
written, copied or moved.
"""

import hashlib
import logging
import os
import posixpath
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.file_types import ALLOWED_EXTENSIONS, guess_mime_type, is_excalidraw
from harbor_clerk.ingest.metadata_extractors.sidecar import SIDECAR_SUFFIX, load_sidecar
from harbor_clerk.mail.parser import EmailParseResult, parse_eml
from harbor_clerk.models.chunk import Chunk
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_heading import DocumentHeading
from harbor_clerk.models.document_page import DocumentPage
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watched_source import (
    failed_for_a_missing_source,
    is_file_exact_case,
    on_disk_name,
    repaired_source_path,
    watched_state,
)
from harbor_clerk.watcher.relocate import apply_move, move_needs_reextract, sidecar_relative_path
from harbor_clerk.watcher.visibility import restore_document, retire_watched_file

logger = logging.getLogger(__name__)

# How long a retired file's document waits to be adopted by a create of the
# same content elsewhere in its folder (the second half of a split rename).
ADOPT_WINDOW_SECS = 10.0

# SHA-256 of empty content. Every 0-byte file produces this digest,
# so it's NOT a unique fingerprint and must never be used as a dedup
# match target — otherwise two transiently-empty files (think: network-share
# copy mid-transfer) would race into the same Document.
# See the early "skip 0-byte files" check in `handle_event` below.
_EMPTY_FILE_SHA256 = hashlib.sha256(b"").digest()


class EventKind(str, Enum):
    created = "created"
    modified = "modified"
    deleted = "deleted"
    # A rename or move inside the folder: ``relative_path`` is the source,
    # ``dest_relative_path`` the destination.
    moved = "moved"


class SkipReason(str, Enum):
    """Why a path was skipped by the watcher.

    ``NOISE`` covers intentional, hard-coded filters that the user never
    expects to ingest (dotfiles, AppleDouble shadow files, ``__MACOSX``
    archive metadata, Excalidraw notes). The UI does not surface these.

    ``UNSUPPORTED_EXTENSION`` covers real files whose extension isn't in
    the allowlist. These ARE surfaced per-folder ("N files not ingested —
    unsupported types: …") so the user can decide whether to extend the
    allowlist or accept the omission.

    ``SIDECAR`` covers a ``.json`` beside a document with the same stem that
    ``ingest/metadata_extractors/sidecar.py`` would accept: the metadata
    sidecar convention. The extract stage attaches its contents to that
    document as ``metadata.sidecar.*``, so indexing the file as a document of
    its own would add a second hit that states the first one's facts (#728).
    Not surfaced: the file is not lost, it is attached.
    """

    NOISE = "noise"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    SIDECAR = "sidecar"


@dataclass
class FileEvent:
    kind: EventKind
    folder_id: uuid.UUID
    relative_path: str
    absolute_path: str
    dest_relative_path: str | None = None
    dest_absolute_path: str | None = None
    # A directory event. Only ``deleted`` is dispatched for one: every file
    # the index holds under it that is no longer on disk is removed.
    is_directory: bool = False
    # The file's sha-256 when the caller has just computed it (a reconcile pass that
    # hashed a new path to pair it); spares hashing it again. For a move, the destination's.
    known_sha: bytes | None = None


def classify_skip(
    relative_path: str,
    absolute_path: str | None = None,
    *,
    sibling_names: "Iterable[str] | SiblingIndex | None" = None,
    sidecar_accepted: "Callable[[Path], bool] | None" = None,
) -> SkipReason | None:
    """Classify why ``relative_path`` would be skipped by the watcher.

    Returns ``None`` if the path WOULD be accepted by ``_should_ignore``
    (i.e. nothing to skip). Otherwise returns the reason, splitting
    intentional-noise filters from real "unsupported extension" rejects
    so callers can count only the latter.

    The name alone decides noise and extension. Whether a ``.json`` is a
    metadata sidecar depends on what sits beside it on disk and on what it
    holds, so that check runs only when ``absolute_path`` is given; name-only
    callers (the API's extension validation) never see ``SIDECAR``. A caller
    that has already listed the directory passes ``sibling_names`` to spare
    a listing per file, and may pass ``sidecar_accepted`` to answer
    ``load_sidecar`` from a cache (the reconcile keys it by the ``.json``'s stat).
    """
    parts = relative_path.split("/")
    if any(p == "__MACOSX" for p in parts):
        return SkipReason.NOISE
    if any(p.startswith("._") for p in parts):
        return SkipReason.NOISE
    if any(p.startswith(".") and p not in ("", ".", "..") for p in parts):
        return SkipReason.NOISE
    if is_excalidraw(relative_path):
        return SkipReason.NOISE
    suffix = Path(relative_path).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return SkipReason.UNSUPPORTED_EXTENSION
    if absolute_path is not None and sidecar_owner_names(absolute_path, sibling_names, sidecar_accepted):
        return SkipReason.SIDECAR
    return None


def is_sidecar(absolute_path: str, sibling_names: "Iterable[str] | SiblingIndex | None" = None) -> bool:
    """True when ``absolute_path`` is a ``.json`` the extract stage would attach to a sibling document."""
    return bool(sidecar_owner_names(absolute_path, sibling_names))


def sidecar_owner_names(
    absolute_path: str,
    sibling_names: "Iterable[str] | SiblingIndex | None" = None,
    sidecar_accepted: "Callable[[Path], bool] | None" = None,
) -> list[str]:
    """Names of the documents beside ``absolute_path`` that it is the sidecar of; empty when it is not one.

    Two conditions, the same two ``SidecarExtractor`` applies from the other
    side: a file the watcher would itself admit sits beside it whose sidecar,
    as the extractor opens it (``Path(source).with_suffix(".json")``), is this
    very file (``report.json`` beside ``report.exe`` is an ordinary document),
    and ``load_sidecar`` accepts the contents (an oversize, malformed,
    non-object or empty ``.json`` is an ordinary document, because the
    extractor would decline it and it must not vanish from both). "This very
    file" is decided on disk, as the extractor's open is: on a
    case-insensitive volume ``report.txt`` reads ``Report.json``, so after
    ``mv Report.txt report.txt`` the ``.json`` is still a sidecar, not a
    duplicate document. A ``.json`` that no longer exists cannot be read, so
    it is judged by exact name here; ``handle_event`` then asks the database
    whether it was read as a sidecar or indexed as a document. One listing
    and one read per call; the caller keeps the names.
    """
    path = Path(absolute_path)
    if path.suffix.lower() != SIDECAR_SUFFIX:
        return []
    owners = _admitted_sibling_names(path, sibling_names)
    if not owners:
        return []
    if not path.exists():
        return owners
    if sidecar_accepted is not None:
        accepted = sidecar_accepted(path)
    else:
        accepted = load_sidecar(path, owner=absolute_path, quiet=True) is not None
    return owners if accepted else []


def _admitted_sibling_names(path: Path, names: "Iterable[str] | SiblingIndex | None" = None) -> list[str]:
    """Files beside ``path`` the watcher would admit whose extractor-side sidecar is ``path``.

    The exact name always counts (``INV-001.pdf`` reads ``INV-001.json``,
    wherever). A name that differs only by case or normalisation counts when
    the file exists and the sibling's ``<stem>.json`` resolves to it on disk,
    which only a case- or normalisation-insensitive volume does. Lists the
    directory unless ``names`` is given. A directory that cannot be listed
    has no siblings: the ordinary path handles the file's own I/O errors.
    """
    if names is None:
        try:
            with os.scandir(path.parent) as entries:
                names = [entry.name for entry in entries if entry.is_file()]
        except OSError:
            return []
    index = names if isinstance(names, SiblingIndex) else SiblingIndex(names)
    exists: bool | None = None
    admitted = []
    for name in index.candidates(path.stem):
        if name == path.name:
            continue
        sibling = Path(name)
        if classify_skip(name) is not None:
            continue
        if sibling.stem == path.stem and path.suffix == SIDECAR_SUFFIX:
            admitted.append(name)
            continue
        if exists is None:
            exists = path.exists()
        if exists and _same_file(path.parent / sibling.with_suffix(SIDECAR_SUFFIX), path):
            admitted.append(name)
    return admitted


def _fold(name: str) -> str:
    """A name as a case- and normalisation-insensitive volume compares it."""
    return unicodedata.normalize("NFC", name).casefold()


class SiblingIndex:
    """One directory's non-``.json`` file names, by folded stem: built once, looked up per ``.json``.

    A reconcile classifies every file on every pass; scanning the whole
    listing for each ``.json`` made a directory of 4,000 document/sidecar
    pairs cost 32 million folds and some 40 s per pass. Building this is one
    fold per name, and each lookup returns only the names whose stem folds the
    same, which the ownership rule then checks exactly as before.
    """

    __slots__ = ("_by_stem",)

    def __init__(self, names: Iterable[str]):
        self._by_stem: dict[str, list[str]] = {}
        for name in names:
            sibling = Path(name)
            if sibling.suffix.lower() == SIDECAR_SUFFIX:
                continue
            self._by_stem.setdefault(_fold(sibling.stem), []).append(name)

    def candidates(self, stem: str) -> list[str]:
        return self._by_stem.get(_fold(stem), [])


def _same_file(a: Path | str, b: Path | str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def sidecar_entry_on_disk(source_path: str) -> str | None:
    """The directory entry the extract stage reads as ``source_path``'s sidecar, or None when there is none.

    ``Path(source).with_suffix(".json")`` as the filesystem resolves it: on a
    case-insensitive volume ``report.txt`` opens ``Report.json``, whose row
    must then be found under that spelling.
    """
    return on_disk_name(str(Path(source_path).with_suffix(SIDECAR_SUFFIX)))


def _should_ignore(relative_path: str) -> bool:
    """True if the path should be filtered out (noise OR unsupported extension).

    Kept as a thin wrapper around ``classify_skip`` so callers that don't
    care about the reason (most of them) stay unchanged. Catches the same
    filesystem noise as before:
      - AppleDouble shadow files (`._<name>`) created when macOS writes
        to non-Mac filesystems (network shares, FAT, exFAT, NFS, SMB).
      - macOS metadata files / directories: `.DS_Store`,
        `.Spotlight-V100`, `.Trashes`, `.fseventsd`, `.TemporaryItems`.
      - `__MACOSX/` archive metadata directories.
      - Other dotfiles (`.git/*`, `.svn/*`, vim swap files, etc.).
      - Excalidraw notes (*.excalidraw.md) — JSON blobs, not prose.
      - Files whose extension isn't in the document allowlist.
    """
    return classify_skip(relative_path) is not None


def sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def file_exists_exact(base: str, relative_path: str) -> bool:
    """``is_file_exact_case`` for ``relative_path`` below the watched root ``base``: every component's spelling."""
    return is_file_exact_case(os.path.join(base, relative_path), base)


def _event_base(event: "FileEvent") -> tuple[str, str]:
    """The watched root and the path below it, as the event spells them.

    An event's absolute path ends with its relative path (FSEvents may spell
    the root as its real path). Should it not, only the leaf can be checked.
    """
    abs_path, rel = event.absolute_path, event.relative_path
    suffix = os.sep + rel.replace("/", os.sep)
    if abs_path.endswith(suffix):
        return abs_path[: -len(suffix)] or os.sep, rel
    return os.path.dirname(abs_path), os.path.basename(abs_path)


def _exists_at_event(event: "FileEvent") -> bool:
    return file_exists_exact(*_event_base(event))


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    if event.kind == EventKind.moved:
        _handle_moved(session, event)
        return
    if event.is_directory:
        if event.kind == EventKind.deleted:
            _handle_directory_deleted(session, event)
        return
    # Noise and unsupported files first: whatever their kind, they are dropped,
    # so the disk checks below (an open and F_GETPATH under the folder lock)
    # are spent only on files the watcher indexes.
    if _should_ignore(event.relative_path):
        logger.debug("watcher: ignored event for %s", event.relative_path)
        return
    if event.kind == EventKind.deleted and _exists_at_event(event):
        # A delete for a file that is on disk is an atomic save (the old inode
        # went, a new one was renamed into its place; macOS flags the replaced
        # inode) or a delete followed by a re-create, reported in either order
        # (#847). Marking it removed would make the document unreadable: the
        # file's state is what is on disk now, so compare its content instead.
        # Only a regular file under that exact spelling counts; a dangling
        # symlink or a directory in its place is a delete.
        logger.debug("watcher: %s reported deleted but exists; treating as modified", event.relative_path)
        event = replace(event, kind=EventKind.modified)
    elif event.kind in (EventKind.created, EventKind.modified) and not _exists_at_event(event):
        # Events are hints: a create or modify for a path that is not on disk
        # under that exact spelling is about a file that is gone. A rename can
        # arrive as two creates, the old name's first (after `mv Old new`, or a
        # case-only `mv Old old`, which `isfile` would still answer for on a
        # case-insensitive volume): retired here, its document is adopted by
        # the other half (`_adopt_recently_retired`) instead of the old path
        # being indexed beside the new one.
        logger.debug("watcher: %s reported %s but is gone; treating as deleted", event.relative_path, event.kind.value)
        event = replace(event, kind=EventKind.deleted)

    existing = (
        session.query(WatchedFile).filter_by(folder_id=event.folder_id, relative_path=event.relative_path).one_or_none()
    )

    # A `.json` the extract stage reads as a sibling document's metadata
    # sidecar (SkipReason.SIDECAR) is not a document. Since that stage is
    # the only reader, the sibling is extracted again so `metadata.sidecar.*`
    # follows the file.
    owners = sidecar_owner_names(event.absolute_path)
    if owners and event.kind != EventKind.deleted:
        # The sidecar may have been indexed as a document before its
        # sibling landed, or before this rule existed; that row is retired.
        _retire_sidecar_row(session, event.folder_id, event.relative_path)
        _reextract_sidecar_owners(session, event, owners)
        return
    if owners:
        # The file is gone, so the database says what it was. A sibling
        # whose metadata carries the sidecar namespace read it; the
        # namespace is dropped in place, since there is nothing left to
        # read and a re-extraction would race the sibling's own deletion
        # (both trashed together, this event first) and strip its chunks.
        # An active row means the `.json` was also a document (an ordinary
        # one the extractor declined, or one indexed before its sibling or
        # by an earlier build): the delete branch below handles that row
        # as for any deleted file.
        _drop_sidecar_namespace(session, event, owners)
        if existing is None or existing.status != WatchedFileStatus.active:
            return

    # Delete events: the file is gone (checked above), so its document leaves
    # search now, as `/watch/remove` does (#604); it comes back through the
    # resurrect branches below if the file returns.
    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            retire_watched_file(existing, session.get(Document, existing.doc_id) if existing.doc_id else None)
        return

    # Skip 0-byte files. Network shares (NFS / SMB / .AppleDouble-bearing
    # filesystems) frequently expose files as 0 bytes mid-copy. Hashing
    # them returns the empty-file sha256 (e3b0c4...), which is identical
    # for every empty file. The next watchdog event (after the file finishes
    # copying) will arrive with real content and be handled normally.
    try:
        if os.path.getsize(event.absolute_path) == 0:
            logger.debug("watcher: skipping 0-byte file %s", event.relative_path)
            return
    except OSError:
        # File vanished between event and stat (renamed away mid-flight, etc.)
        return

    # Create / modified events: compute sha then dispatch
    sha = event.known_sha if event.known_sha is not None else sha256_of(event.absolute_path)

    # Defensive: even if the size check above somehow lets one through
    # (race between getsize and read; truncate-to-zero between syscalls),
    # never accept the empty-file sha as a real fingerprint. Same harm
    # as the size check prevents.
    if sha == _EMPTY_FILE_SHA256:
        logger.debug("watcher: skipping event with empty-file sha for %s", event.relative_path)
        return

    # This file's `<stem>.json`, if one was indexed as a document (it landed
    # first, or before this rule existed), is its sidecar from now on. Runs
    # before the no-op branch so the initial scan after an upgrade retires
    # the rows the old rule created, and after the empty-file returns above:
    # a file still mid-copy will be back with content, and retires it then.
    # Only a file the extract stage will read is retired, by the same
    # predicate: a same-stem `.json` it would decline is a document in its
    # own right, and a missing one has its own delete event.
    if Path(event.relative_path).suffix.lower() != SIDECAR_SUFFIX:
        entry = sidecar_entry_on_disk(event.absolute_path)
        sidecar = Path(event.absolute_path).with_suffix(SIDECAR_SUFFIX)
        if entry is not None and load_sidecar(sidecar, owner=event.absolute_path, quiet=True) is not None:
            rel_dir = posixpath.dirname(event.relative_path)
            _retire_sidecar_row(session, event.folder_id, posixpath.join(rel_dir, entry) if rel_dir else entry)

    # Branch 1: existing+active+same → no-op, but for an aliased source_path, which is
    # canonicalized so the document verifies as read in place (the startup scan
    # visits every file, so this heals rows older clients stored).
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 == sha:
        _repair_source_path(session, existing)
        _bring_up_to_date(session, existing, sha)
        return

    # Branch 2: existing+active+different → reprocess in place
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        return

    # Branch 3: existing+removed+same → resurrect, no re-ingest
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 == sha:
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        _unhide_document(session, existing.doc_id)
        _repair_source_path(session, existing)
        _bring_up_to_date(session, existing, sha)
        return

    # Branch 4: existing+removed+different → resurrect + reprocess
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        _unhide_document(session, existing.doc_id)
        return

    # A file that just vanished elsewhere in this folder with this content
    # moved here: it keeps its document (a rename whose halves arrived apart).
    if _adopt_recently_retired(session, event, sha):
        return

    # No existing row → create Document + WatchedFile + extract job
    _create_doc_and_enqueue(session, event, sha)


def _adopt_recently_retired(session: Session, event: FileEvent, sha: bytes) -> bool:
    """Give a new file the document of one retired moments ago in the same folder with the same content.

    A rename can reach the watcher as two halves: a delete (or a create for the
    old name, which is gone: a delete) then a create, including a case-only
    rename split across FSEvents callbacks, or a move whose destination is
    already gone again. The retired row is adopted only within
    ADOPT_WINDOW_SECS, only when its own path is gone (otherwise this is a
    copy), and only for identical content, so the adopted document's text is
    exactly this file's.

    It does not carry documents through a swap via a temp name (``mv a t;
    mv b a; mv t b``): each move's source is back on disk when it is handled,
    so ``_handle_moved`` compares both paths in place, and each path keeps its
    document with the other's content, as on main.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=ADOPT_WINDOW_SECS)
    candidates = (
        session.query(WatchedFile)
        .filter(
            WatchedFile.folder_id == event.folder_id,
            WatchedFile.status == WatchedFileStatus.removed,
            WatchedFile.sha256 == sha,
            WatchedFile.removed_at >= cutoff,
            WatchedFile.relative_path != event.relative_path,
        )
        .order_by(WatchedFile.removed_at.desc())
        .all()
    )
    base, _rel = _event_base(event)
    for wf in candidates:
        if file_exists_exact(base, wf.relative_path):
            continue
        doc = session.get(Document, wf.doc_id) if wf.doc_id else None
        old_rel = wf.relative_path
        reextract = move_needs_reextract(old_rel, event.relative_path, doc, event.absolute_path)
        apply_move(wf, doc, event.relative_path, event.absolute_path)
        wf.status = WatchedFileStatus.active
        wf.removed_at = None
        restore_document(doc)
        if doc is not None and reextract and not _extract_queued(session, doc.doc_id):
            _reprocess_doc(session, doc.doc_id, sha, event.absolute_path)
        logger.info("watcher: %s is %s moved; document kept", event.relative_path, old_rel)
        return True
    return False


def _handle_moved(session: Session, event: FileEvent) -> None:
    """A file renamed or moved inside the watched folder (#847).

    A known file keeps its document: the ``WatchedFile`` row and the document's
    paths follow it in place (``relocate.apply_move``, shared with
    ``/watch/rename``), so citations and chat history keep resolving and an
    unchanged file is not re-indexed. The destination is then compared like
    any modified file: changed content is reprocessed under the same doc_id,
    and so is a move that changes its type or the metadata sidecar it reads
    (``relocate.move_needs_reextract``). A sidecar the old name owned is then
    judged again: with its document gone from beside it, it is an ordinary
    document (or another sibling's sidecar), never in neither place.

    The event is a hint, checked against the disk before anything moves: a
    source still there under its exact spelling (and not the destination's
    file) was put back by a later rename, as a safe-save swapping a temp file
    in does, so both paths are compared by path instead (main's behaviour).

    A move onto a path the index holds a row for (an atomic save renaming a
    temp file over the original, ``mv a b`` over a live ``b``, or a path
    retired moments ago) keeps the destination's document, which is the one
    cited by that path; the source's row is retired as a deleted file, still
    there to be adopted. Nothing here deletes a row or a document. A move from
    a name the watcher ignores (an editor's temp file) is a modify of the
    destination; a move to one is a delete of the source. A move of a sidecar
    itself takes the delete-and-create route, where its rules live.
    """
    dest_rel, dest_abs = event.dest_relative_path, event.dest_absolute_path
    assert dest_rel is not None and dest_abs is not None, "a moved event names its destination"
    src_deleted = replace(
        event, kind=EventKind.deleted, dest_relative_path=None, dest_absolute_path=None, known_sha=None
    )
    dest_modified = FileEvent(EventKind.modified, event.folder_id, dest_rel, dest_abs, known_sha=event.known_sha)

    if _should_ignore(dest_rel):
        handle_event(session, src_deleted)
        _rejudge_old_sidecar(session, event)
        return
    if _should_ignore(event.relative_path) or sidecar_owner_names(event.absolute_path) or sidecar_owner_names(dest_abs):
        handle_event(session, src_deleted)
        handle_event(session, dest_modified)
        return

    if _exists_at_event(src_deleted) and not _same_file(event.absolute_path, dest_abs):
        logger.info("watcher: %s moved to %s, but it is still there; compared by path", event.relative_path, dest_rel)
        handle_event(session, replace(src_deleted, kind=EventKind.modified))
        handle_event(session, dest_modified)
        return

    src_wf = (
        session.query(WatchedFile).filter_by(folder_id=event.folder_id, relative_path=event.relative_path).one_or_none()
    )
    dest_wf = session.query(WatchedFile).filter_by(folder_id=event.folder_id, relative_path=dest_rel).one_or_none()
    if src_wf is None or dest_wf is not None:
        handle_event(session, src_deleted)
        handle_event(session, dest_modified)
        _rejudge_old_sidecar(session, event)
        return

    doc = session.get(Document, src_wf.doc_id) if src_wf.doc_id else None
    reextract = move_needs_reextract(event.relative_path, dest_rel, doc, dest_abs)
    apply_move(src_wf, doc, dest_rel, dest_abs)
    session.flush()
    if not file_exists_exact(*_event_base(dest_modified)):
        # Gone again by the time this is handled: the next leg of a chain of
        # renames was dropped, or the file was deleted. The row must not stay
        # active at a missing path (a phantom in search). Retired here, its
        # document is adopted by a create of the same content in this folder
        # moments later (`_adopt_recently_retired`), or reaped like any other.
        retire_watched_file(src_wf, doc)
        logger.info("watcher: %s moved to %s, which is gone again; retired", event.relative_path, dest_rel)
        _rejudge_old_sidecar(session, event)
        return
    logger.info("watcher: %s moved to %s; document kept", event.relative_path, dest_rel)

    seq_before = doc.pipeline_seq if doc is not None else None
    # Same content: a no-op, or a resurrection for a row a racing delete had
    # retired. Changed content: reprocessed in place. This is also where a
    # `.json` beside the new name that was indexed as a document is retired,
    # now that it is this file's sidecar.
    handle_event(session, dest_modified)
    if doc is not None and reextract and doc.pipeline_seq == seq_before and not _extract_queued(session, doc.doc_id):
        _reprocess_doc(session, doc.doc_id, src_wf.sha256, dest_abs)
    _rejudge_old_sidecar(session, event)


def _rejudge_old_sidecar(session: Session, event: FileEvent) -> None:
    """Re-run the ``.json`` beside a moved file's old name through ``handle_event``.

    It was that file's sidecar, read into its document and not indexed. With
    the file gone from beside it, it is either an ordinary document or the
    sidecar of another same-stem sibling; ``handle_event`` decides which.
    """
    if Path(event.relative_path).suffix.lower() == SIDECAR_SUFFIX:
        return
    base, _rel = _event_base(event)
    old_target = os.path.join(base, sidecar_relative_path(event.relative_path))
    if event.dest_relative_path is not None:
        new_target = os.path.join(base, sidecar_relative_path(event.dest_relative_path))
        if old_target == new_target or _same_file(old_target, new_target):
            # A case-only rename on a case-insensitive volume: the new name
            # reads the same sidecar, which stays where it is.
            return
    entry = sidecar_entry_on_disk(os.path.join(base, event.relative_path))
    if entry is None:
        return
    rel_dir = posixpath.dirname(event.relative_path)
    old_rel = posixpath.join(rel_dir, entry) if rel_dir else entry
    handle_event(session, FileEvent(EventKind.modified, event.folder_id, old_rel, os.path.join(base, old_rel)))


def _extract_queued(session: Session, doc_id: uuid.UUID) -> bool:
    return (
        session.query(IngestionJob).filter_by(doc_id=doc_id, stage=JobStage.extract, status=JobStatus.queued).first()
        is not None
    )


def _handle_directory_deleted(session: Session, event: FileEvent) -> None:
    """A subdirectory deleted or moved out of the folder: retire every file the index holds under it.

    FSEvents reports a directory moved out of the tree as the directory alone,
    with no event per file, so without this its files would stay active. Each
    file is checked on disk first, every path component exactly, as for a
    single delete: after ``mv Invoices invoices`` on a case-insensitive volume
    the old spelling still resolves. The watched root itself never comes here:
    its loss means re-attaching, never removing every document
    (``FolderObserver``).
    """
    rel = event.relative_path.strip("/")
    if rel in ("", "."):
        return
    base, _rel = _event_base(event)
    prefix = rel + "/"
    rows = (
        session.query(WatchedFile)
        .filter(
            WatchedFile.folder_id == event.folder_id,
            WatchedFile.status == WatchedFileStatus.active,
            WatchedFile.relative_path.startswith(prefix, autoescape=True),
        )
        .all()
    )
    retired = 0
    for wf in rows:
        if file_exists_exact(base, wf.relative_path):
            continue
        retire_watched_file(wf, session.get(Document, wf.doc_id) if wf.doc_id else None)
        retired += 1
    if retired:
        logger.info("watcher: directory %s removed; retired %d files under it", rel, retired)


def _repair_source_path(session: Session, watched_file: WatchedFile) -> None:
    """Rewrite the document's source_path to this watched file's canonical path when
    it is anything else and that file is present (``watched_source.repaired_source_path``)."""
    if watched_file.doc_id is None:
        return
    folder = session.get(WatchedFolder, watched_file.folder_id)
    doc = session.get(Document, watched_file.doc_id)
    if folder is None or doc is None:
        return
    repaired = repaired_source_path(doc.source_path, folder.path, watched_file.relative_path)
    if repaired is not None:
        logger.info("watcher: canonicalized the source_path of doc %s", doc.doc_id)
        doc.source_path = repaired


def _bring_up_to_date(session: Session, watched_file: WatchedFile, sha: bytes) -> None:
    """Queue the document's extract again, from its file, when its index does not
    reflect the file it is read from:

    - its last run failed because its file could not be read
      (``watched_source.failed_for_a_missing_source``): mid atomic save, or deleted
      and later restored with the same bytes. Other errors are left;
    - its sha256 is not the watched file's: a new version confirmed before that was
      refused (#859) indexed an upload, while reads now come from the file.

    The unchanged-file branches would otherwise leave either for good.
    """
    if watched_file.doc_id is None:
        return
    doc = session.get(Document, watched_file.doc_id)
    if doc is None:
        return
    stale = doc.sha256 != sha
    if not stale and not failed_for_a_missing_source(doc):
        return
    # Only a document that now verifies as read in place: one whose repair was
    # declined stays as it is (it would fail again), and the path handed on is the
    # verified one, never a source_path that did not verify.
    in_place = watched_state(session, doc).path
    if in_place is None:
        return
    logger.info("watcher: queueing doc %s again (%s)", doc.doc_id, "stale index" if stale else "after a failed run")
    _reprocess_doc(session, doc.doc_id, sha, in_place)


def _reprocess_doc(session: Session, doc_id: uuid.UUID, sha: bytes, source_path: str) -> None:
    """Bump pipeline_seq, DELETE child rows, set pipeline_status=queued, enqueue extract.

    The doc_id is preserved across content changes — chat conversations and
    citations referencing this doc keep resolving to the (now updated) doc.
    """
    doc = session.query(Document).filter_by(doc_id=doc_id).one()
    doc.pipeline_seq = doc.pipeline_seq + 1
    doc.sha256 = sha
    doc.source_path = source_path
    doc.pipeline_status = PipelineStatus.queued
    doc.error = None
    # For .eml: re-parse so the email_* columns (subject, from, to, date,
    # etc.) reflect the new bytes. Otherwise an in-place edit that, say,
    # corrects a misspelled subject would leave the old subject in the DB.
    # Best-effort — parse failure leaves the existing values intact.
    if (doc.mime_type or guess_mime_type(source_path)) == "message/rfc822":
        parsed = _try_parse_eml(source_path)
        if parsed is not None:
            _apply_email_fields(doc, parsed)
    # Delete child rows explicitly (FKs have CASCADE but we do it here for
    # clarity and to avoid relying on DB-level CASCADE for application state).
    session.query(Chunk).filter_by(doc_id=doc_id).delete()
    session.query(Entity).filter_by(doc_id=doc_id).delete()
    session.query(DocumentPage).filter_by(doc_id=doc_id).delete()
    session.query(DocumentHeading).filter_by(doc_id=doc_id).delete()
    session.query(IngestionJob).filter_by(doc_id=doc_id).delete()
    session.add(
        IngestionJob(
            doc_id=doc_id,
            stage=JobStage.extract,
            status=JobStatus.queued,
            pipeline_seq=doc.pipeline_seq,
        )
    )


def _retire_sidecar_row(session: Session, folder_id: uuid.UUID, relative_path: str) -> bool:
    """Mark the WatchedFile at ``relative_path`` removed and hide its document, if it is active.

    The same retirement as a deleted file and ``api/routes/watch.py::remove_file``
    (``retire_watched_file``): a sidecar was never a document, so it leaves
    search now. An admin's "deleted" outranks it and is left alone. Returns
    True when a row was retired.
    """
    wf = (
        session.query(WatchedFile)
        .filter_by(folder_id=folder_id, relative_path=relative_path, status=WatchedFileStatus.active)
        .one_or_none()
    )
    if wf is None:
        return False
    retire_watched_file(wf, session.get(Document, wf.doc_id) if wf.doc_id else None)
    logger.info("watcher: %s is a metadata sidecar of a sibling document; retired its own document row", relative_path)
    return True


def _active_owner_docs(session: Session, event: FileEvent, owner_names: Iterable[str]):
    """Yield ``(relative_path, WatchedFile, Document)`` for each owner that is a live, visible document.

    A hidden document (admin "deleted", or retired) is not the watcher's to
    touch; the worker would not claim its jobs anyway.
    """
    rel_dir = posixpath.dirname(event.relative_path)
    for name in owner_names:
        rel = posixpath.join(rel_dir, name) if rel_dir else name
        wf = (
            session.query(WatchedFile)
            .filter_by(folder_id=event.folder_id, relative_path=rel, status=WatchedFileStatus.active)
            .one_or_none()
        )
        if wf is None or wf.doc_id is None:
            continue
        doc = session.get(Document, wf.doc_id)
        if doc is None or doc.status != "active":
            continue
        yield rel, wf, doc


def _reextract_sidecar_owners(session: Session, event: FileEvent, owner_names: Iterable[str]) -> int:
    """Queue a fresh extraction of the sidecar event's owners (its admitted same-stem siblings).

    The extract stage is the only reader of the sidecar, so a sidecar that
    is created or edited after its document was extracted would otherwise
    leave `metadata.sidecar.*` frozen (#760). An extraction that is still
    queued will read the file as it now is, so it is left alone rather than
    re-queued with a bumped generation; that is also what keeps a document
    and its sidecar landing together from being extracted twice (one already
    running is replaced, and may run twice: a stale snapshot of the sidecar
    is worse). An owner whose `metadata.sidecar` already holds exactly what
    the file now says (the sidecar moved along with it, or was touched) is
    left alone: re-extracting takes it out of search until reprocessed.
    Returns how many documents were re-queued.
    """
    parent = Path(event.absolute_path).parent
    content = load_sidecar(Path(event.absolute_path), owner=event.absolute_path, quiet=True)
    requeued = 0
    for rel, wf, doc in _active_owner_docs(session, event, owner_names):
        if content is not None and (doc.doc_metadata or {}).get("sidecar") == content:
            continue
        pending = (
            session.query(IngestionJob)
            .filter_by(doc_id=wf.doc_id, stage=JobStage.extract, status=JobStatus.queued)
            .first()
        )
        if pending is not None:
            continue
        _reprocess_doc(session, wf.doc_id, wf.sha256, str(parent / Path(rel).name))
        requeued += 1
        logger.info("watcher: sidecar %s changed; re-extracting %s", event.relative_path, rel)
    return requeued


def _drop_sidecar_namespace(session: Session, event: FileEvent, owner_names: Iterable[str]) -> int:
    """Remove `metadata.sidecar.*` (and its provenance stamp) from the owners of a deleted sidecar.

    The inverse of what the extract stage did when it read the file. Done in
    place rather than by re-extraction: there is nothing on disk to read, and
    a re-extraction would race the owner's own deletion when document and
    sidecar are trashed together with the sidecar's event handled first,
    stripping the owner's chunks and queueing an extract of a path that is
    gone. Returns how many documents were changed.
    """
    dropped = 0
    for rel, _wf, doc in _active_owner_docs(session, event, owner_names):
        meta = doc.doc_metadata or {}
        if "sidecar" not in meta:
            continue
        # A new dict, not a mutation: JSONB columns are compared by value on
        # flush, and an in-place change would go unnoticed.
        new_meta = {key: value for key, value in meta.items() if key != "sidecar"}
        provenance = {k: v for k, v in (new_meta.get("_source_provenance") or {}).items() if k != "sidecar"}
        if provenance:
            new_meta["_source_provenance"] = provenance
        else:
            new_meta.pop("_source_provenance", None)
        doc.doc_metadata = new_meta or None
        dropped += 1
        logger.info("watcher: sidecar %s deleted; dropped metadata.sidecar from %s", event.relative_path, rel)
    return dropped


def _unhide_document(session: Session, doc_id: uuid.UUID | None) -> None:
    """Undo a retire when a resurrected row's document is hidden.

    A removed row has its document set to "removed" (a deleted file, a
    sidecar whose sibling has since gone, or the API's /watch/remove); a live
    WatchedFile pointing at a hidden document would be unreachable. Only that
    state is undone (``restore_document``): an admin's "deleted" (DELETE
    /docs/{id}) is not the watcher's to revive, whatever happens on disk.
    """
    if doc_id is None:
        return
    restore_document(session.get(Document, doc_id))


def _try_parse_eml(absolute_path: str) -> EmailParseResult | None:
    """Best-effort `.eml` parse from disk. Returns None on any read or parse
    failure — the caller falls through to a generic Document with email_*
    columns NULL. `.eml` files are typically small; the extra read here on
    top of the SHA-streaming read is page-cache-cheap in practice."""
    try:
        with open(absolute_path, "rb") as fh:
            return parse_eml(fh.read())
    except Exception as exc:
        logger.warning("watcher: parse_eml failed for %s: %s", absolute_path, exc)
        return None


def _apply_email_fields(doc: Document, parsed: EmailParseResult) -> None:
    """Copy parsed-email metadata onto an existing Document row.

    Mirrors `mail/ingest.py::create_email_document` for the watched-folder
    ingest path. Sets the email_* columns + title + created_at; we don't
    touch sha256, source_path, mime_type, or updated_at. `updated_at` is
    intentionally left to the caller — on a reprocess (in-place .eml edit)
    it would be wrong to slide updated_at backwards to the email's send
    date when the record was just touched now. The IMAP path also sets
    `email_label_path` (Gmail label name); watched-folder ingest has
    `folder_id` on the WatchedFile row for that role, so email_label_path
    stays NULL here.
    """
    doc.title = parsed.subject
    doc.email_message_id = parsed.message_id
    doc.email_thread_id = parsed.thread_id
    doc.email_from_address = parsed.from_address
    doc.email_from_name = parsed.from_name
    doc.email_subject = parsed.subject
    doc.email_to_addresses = parsed.to_addresses or None
    doc.email_cc_addresses = parsed.cc_addresses or None
    doc.email_date_sent = parsed.date_sent
    # Match IMAP-path semantics: created_at = email send date, so the
    # Documents page sorts emails by when they were sent, not when they
    # were dropped into the watched folder. Stays semantically correct
    # on reprocess too — `created_at` describes the email content, and
    # the new bytes' Date header is what we want.
    if parsed.date_sent is not None:
        doc.created_at = parsed.date_sent


def _create_doc_and_enqueue(session: Session, event: FileEvent, sha: bytes) -> None:
    filename = Path(event.absolute_path).name
    mime = guess_mime_type(filename)

    # mime_type was historically left NULL on the watched-folder path (the
    # primary ingest path post Stage-2 watched-folder-first refactor), which
    # broke the Observatory's file-type breakdown. The extract stage already
    # expects mime_type to be set (`doc.mime_type or ""` at extract.py:254),
    # so populating here also tightens that contract.
    doc = Document(
        title=Path(event.absolute_path).stem,
        canonical_filename=filename,
        status="active",
        sha256=sha,
        source_path=event.absolute_path,
        mime_type=mime,
        pipeline_status=PipelineStatus.queued,
    )

    # For .eml files: parse RFC 5322 headers at ingest so PR #414's preamble
    # injection and the `email.*` metadata_filter namespace actually fire on
    # watched-folder docs (not just IMAP-synced ones). The previous version
    # left email_* NULL, which silently disabled both features for every
    # .eml ever loaded via a watched folder.
    if mime == "message/rfc822":
        parsed = _try_parse_eml(event.absolute_path)
        if parsed is not None:
            _apply_email_fields(doc, parsed)
            # Pin updated_at = date_sent at first-create only (matches IMAP
            # `create_email_document`). `_apply_email_fields` deliberately
            # doesn't touch updated_at because the reprocess caller would
            # otherwise slide updated_at backwards on every in-place edit.
            if parsed.date_sent is not None:
                doc.updated_at = parsed.date_sent

    session.add(doc)
    session.flush()
    session.add(
        WatchedFile(
            folder_id=event.folder_id,
            relative_path=event.relative_path,
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.queued,
            pipeline_seq=doc.pipeline_seq or 0,
        )
    )
