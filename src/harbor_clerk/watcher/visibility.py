"""How a watched file's absence or return shows on its document. Shared by the watcher and the API.

A watched file that is gone (deleted, moved out of the folder, retired as a
metadata sidecar, or removed through ``POST /api/watch/remove``) has its
``WatchedFile`` marked ``removed`` and its document hidden (``status =
"removed"``), so it leaves search, chat and MCP at once (#604). The reaper in
``api/app.py`` hard-deletes both 30 days later. A file that comes back
restores the document. An admin's ``"deleted"`` (``DELETE /docs/{id}``)
outranks both directions and is never changed here.

Pure functions over loaded rows: no session, no I/O, so the sync watcher and
the async API call the same code.
"""

from datetime import UTC, datetime

from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus


def retire_watched_file(wf: WatchedFile, doc: Document | None) -> None:
    """Mark ``wf`` removed and hide its document, unless an admin already deleted it."""
    wf.status = WatchedFileStatus.removed
    wf.removed_at = datetime.now(UTC)
    if doc is not None and doc.status == "active":
        doc.status = "removed"


def restore_document(doc: Document | None) -> None:
    """Undo ``retire_watched_file``'s hiding. Only ``"removed"`` is undone, never an admin's ``"deleted"``."""
    if doc is not None and doc.status == "removed":
        doc.status = "active"
