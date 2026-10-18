"""What moving a watched file does to its rows. Shared by the watcher's moved events and ``POST /watch/rename``.

A file that is renamed or moved inside its folder keeps its document: the
``WatchedFile`` row and the document's paths follow it in place, so citations
and chat history keep resolving (#847). Whether it must be extracted again is
decided here too, since both callers need the same answer.

Pure functions over loaded rows, plus reads of the destination's metadata
sidecar: no session, so the sync watcher and the async API call the same code.
"""

import os
import posixpath
from pathlib import Path

from harbor_clerk.file_types import guess_mime_type
from harbor_clerk.ingest.metadata_extractors.sidecar import SIDECAR_SUFFIX, load_sidecar
from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile


def sidecar_relative_path(relative_path: str) -> str:
    """The relative path ``SidecarExtractor`` would read for ``relative_path``."""
    return posixpath.splitext(relative_path)[0] + SIDECAR_SUFFIX


def apply_move(wf: WatchedFile, doc: Document | None, new_relative_path: str, new_absolute_path: str) -> None:
    """Point ``wf`` and its document at the new path.

    The file name, and a title the watcher derived from the old name, follow;
    a title the extract stage set (a PDF's metadata, an email's subject)
    stays. A changed extension changes the mime type, which decides how the
    file is extracted.
    """
    old_relative_path = wf.relative_path
    wf.relative_path = new_relative_path
    if doc is None:
        return
    doc.source_path = new_absolute_path
    doc.canonical_filename = Path(new_absolute_path).name
    if doc.title == Path(old_relative_path).stem:
        doc.title = Path(new_relative_path).stem
    if type_changed(old_relative_path, new_relative_path):
        doc.mime_type = guess_mime_type(doc.canonical_filename)


def type_changed(old_relative_path: str, new_relative_path: str) -> bool:
    return Path(old_relative_path).suffix.lower() != Path(new_relative_path).suffix.lower()


def sidecar_changes_with_move(
    old_relative_path: str, new_relative_path: str, doc: Document | None, new_absolute_path: str
) -> bool:
    """True when the move changes which metadata sidecar the document reads.

    The extract stage reads ``<stem>.json`` beside the file, so a move that
    changes the stem or the directory changes the sidecar: one read under the
    old name (``metadata.sidecar`` is set) is stale, and one waiting under the
    new name has not been read.
    """
    old_sidecar, new_sidecar = sidecar_relative_path(old_relative_path), sidecar_relative_path(new_relative_path)
    if old_sidecar == new_sidecar:
        return False
    # A case-only rename on a case-insensitive volume reads the very same file.
    if new_absolute_path.endswith(new_relative_path):
        base = new_absolute_path[: len(new_absolute_path) - len(new_relative_path)]
        try:
            if os.path.samefile(base + old_sidecar, base + new_sidecar):
                return False
        except OSError:
            pass
    if doc is not None and "sidecar" in (doc.doc_metadata or {}):
        return True
    sidecar = Path(new_absolute_path).with_suffix(SIDECAR_SUFFIX)
    return load_sidecar(sidecar, owner=new_absolute_path, quiet=True) is not None


def move_needs_reextract(
    old_relative_path: str, new_relative_path: str, doc: Document | None, new_absolute_path: str
) -> bool:
    """Whether an unchanged file must be extracted again after the move: a new type, or a different sidecar."""
    return type_changed(old_relative_path, new_relative_path) or sidecar_changes_with_move(
        old_relative_path, new_relative_path, doc, new_absolute_path
    )
