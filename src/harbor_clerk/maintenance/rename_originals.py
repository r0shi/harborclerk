"""One-shot: rename originals/versions/<version_id>/<f> keys to
originals/docs/<doc_id>/<f> keys.

Idempotent: walks all `originals/versions/` objects in the bucket and
either renames each one (if its filename matches a Document's
original_object_key) or deletes it (orphan from a deleted/non-latest
version row). Returns (renamed_count, orphans_deleted_count).

Run at app startup; logs and skips cleanly if nothing remains under the
old prefix.

Run manually:
    uv run python -m harbor_clerk.maintenance.rename_originals
"""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from harbor_clerk.config import get_settings
from harbor_clerk.models import Document
from harbor_clerk.storage import get_storage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def rename_all(session: "Session") -> tuple[int, int]:
    """Walk old-prefix keys; rename or delete. Returns (renamed, orphans_deleted)."""
    backend = get_storage()
    settings = get_settings()
    bucket = settings.minio_bucket  # same bucket name for both backends

    # Map filename → [(doc_id_str, expected_new_key)] from the DB rows.
    # Multiple Documents could share a filename; if so we can't be sure
    # which old key matches which doc, so we skip those (logged).
    by_filename: dict[str, list[tuple[str, str]]] = defaultdict(list)
    docs = session.query(Document).filter(Document.original_object_key.isnot(None)).all()
    for doc in docs:
        # original_object_key is now "originals/docs/<doc_id>/<filename>"
        if "/" not in doc.original_object_key:
            continue
        filename = doc.original_object_key.rsplit("/", 1)[-1]
        by_filename[filename].append((str(doc.doc_id), doc.original_object_key))

    renamed = 0
    orphans = 0
    skipped_ambiguous = 0

    # Walk old prefix — list_objects returns list[dict] with {"key": ..., "size": ...}
    for obj in backend.list_objects(bucket, prefix="originals/versions/", recursive=True):
        old_key = obj["key"]
        filename = old_key.rsplit("/", 1)[-1] if "/" in old_key else old_key

        candidates = by_filename.get(filename, [])
        if len(candidates) == 1:
            doc_id, new_key = candidates[0]
            try:
                backend.copy_and_delete(bucket, old_key, bucket, new_key)
                renamed += 1
                logger.info("rename_originals: %s → %s", old_key, new_key)
            except Exception:
                logger.exception("rename_originals: failed to move %s → %s", old_key, new_key)
        elif len(candidates) > 1:
            logger.warning(
                "rename_originals: ambiguous filename %s — %d candidates; skipping",
                filename,
                len(candidates),
            )
            skipped_ambiguous += 1
        else:
            # No Document references this filename — orphan, delete it
            try:
                backend.remove_object(bucket, old_key)
                orphans += 1
                logger.info("rename_originals: orphan deleted %s", old_key)
            except Exception:
                logger.exception("rename_originals: failed to delete orphan %s", old_key)

    logger.info(
        "rename_originals: %d renamed, %d orphans deleted, %d skipped (ambiguous)",
        renamed,
        orphans,
        skipped_ambiguous,
    )
    return renamed, orphans


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from harbor_clerk.db_sync import get_sync_session

    s = get_sync_session()
    try:
        rename_all(s)
    finally:
        s.close()
