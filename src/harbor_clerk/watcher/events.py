"""Filesystem-event → database-write translation.

No watchdog imports, no I/O scheduling. Caller passes in synthetic FileEvent
records; this module does the database work in the provided session. Caller
is responsible for commit. It does read the filesystem: the event's file is
hashed, an `.eml` is parsed, and a `.json` is judged a metadata sidecar or a
document by listing its directory and reading it (`sidecar_owner_names`).
Nothing under a watched folder is ever written, copied or moved.
"""

import hashlib
import logging
import os
import posixpath
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

logger = logging.getLogger(__name__)

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


def classify_skip(
    relative_path: str,
    absolute_path: str | None = None,
    *,
    sibling_names: Iterable[str] | None = None,
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
    a listing per file.
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
    if absolute_path is not None and is_sidecar(absolute_path, sibling_names=sibling_names):
        return SkipReason.SIDECAR
    return None


def is_sidecar(absolute_path: str, sibling_names: Iterable[str] | None = None) -> bool:
    """True when ``absolute_path`` is a ``.json`` the extract stage would attach to a sibling document."""
    return bool(sidecar_owner_names(absolute_path, sibling_names))


def sidecar_owner_names(absolute_path: str, sibling_names: Iterable[str] | None = None) -> list[str]:
    """Names of the documents beside ``absolute_path`` that it is the sidecar of; empty when it is not one.

    Two conditions, the same two ``SidecarExtractor`` applies from the other
    side: a file with exactly the same stem that the watcher would itself
    admit sits beside it (``report.json`` beside ``report.exe`` is an
    ordinary document), and ``load_sidecar`` accepts the contents (an
    oversize, malformed, non-object or empty ``.json`` is an ordinary
    document, because the extractor would decline it and it must not vanish
    from both). A ``.json`` that no longer exists cannot be read, so it is
    judged by its siblings here; ``handle_event`` then asks the database
    whether it was read as a sidecar or indexed as a document. One listing
    and one read per call; the caller keeps the names.
    """
    path = Path(absolute_path)
    if path.suffix != SIDECAR_SUFFIX:
        return []
    owners = _admitted_sibling_names(path, sibling_names)
    if not owners:
        return []
    if path.exists() and load_sidecar(path, owner=absolute_path, quiet=True) is None:
        return []
    return owners


def _admitted_sibling_names(path: Path, names: Iterable[str] | None = None) -> list[str]:
    """Files beside ``path`` with its exact stem that the watcher would admit, by name.

    Lists the directory unless ``names`` is given. A directory that cannot
    be listed has no siblings: the ordinary path handles the file's own I/O
    errors.
    """
    if names is None:
        try:
            with os.scandir(path.parent) as entries:
                names = [entry.name for entry in entries if entry.is_file()]
        except OSError:
            return []
    admitted = []
    for name in names:
        if name == path.name:
            continue
        sibling = Path(name)
        if sibling.stem != path.stem or sibling.suffix == SIDECAR_SUFFIX:
            continue
        if classify_skip(name) is None:
            admitted.append(name)
    return admitted


def _sidecar_relative_path(relative_path: str) -> str:
    """The relative path ``SidecarExtractor`` would read for ``relative_path``."""
    return posixpath.splitext(relative_path)[0] + SIDECAR_SUFFIX


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


def _sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    if _should_ignore(event.relative_path):
        logger.debug("watcher: ignored event for %s", event.relative_path)
        return

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

    # Delete events
    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(UTC)
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
    sha = _sha256_of(event.absolute_path)

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
    if Path(event.relative_path).suffix != SIDECAR_SUFFIX:
        sidecar = Path(event.absolute_path).with_suffix(SIDECAR_SUFFIX)
        if load_sidecar(sidecar, owner=event.absolute_path, quiet=True) is not None:
            _retire_sidecar_row(session, event.folder_id, _sidecar_relative_path(event.relative_path))

    # Branch 1: existing+active+same → no-op
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 == sha:
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
        return

    # Branch 4: existing+removed+different → resurrect + reprocess
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        _unhide_document(session, existing.doc_id)
        return

    # No existing row → create Document + WatchedFile + extract job
    _create_doc_and_enqueue(session, event, sha)


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

    Mirrors ``api/routes/watch.py::remove_file`` rather than the delete
    branch above: a sidecar was never a document, so it leaves search now
    instead of lingering until the reaper hard-deletes the row 30 days on.
    Returns True when a row was retired.
    """
    wf = (
        session.query(WatchedFile)
        .filter_by(folder_id=folder_id, relative_path=relative_path, status=WatchedFileStatus.active)
        .one_or_none()
    )
    if wf is None:
        return False
    wf.status = WatchedFileStatus.removed
    wf.removed_at = datetime.now(UTC)
    doc = session.get(Document, wf.doc_id) if wf.doc_id else None
    # An admin's "deleted" (DELETE /docs/{id}) outranks this; it is left alone.
    if doc is not None and doc.status == "active":
        doc.status = "removed"
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
    is worse). Returns how many documents were re-queued.
    """
    parent = Path(event.absolute_path).parent
    requeued = 0
    for rel, wf, _doc in _active_owner_docs(session, event, owner_names):
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

    A row can be marked removed with its document set to "removed" (a sidecar
    whose sibling has since gone, or the API's /watch/remove); a live
    WatchedFile pointing at a hidden document would be unreachable. Only that
    state is undone: an admin's "deleted" (DELETE /docs/{id}) is not the
    watcher's to revive, whatever happens to the file on disk.
    """
    if doc_id is None:
        return
    doc = session.get(Document, doc_id)
    if doc is not None and doc.status == "removed":
        doc.status = "active"


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
