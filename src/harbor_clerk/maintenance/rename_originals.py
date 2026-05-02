"""One-shot storage-key migrations.

This module covers two distinct migration paths:

1. **Legacy pre-Stage-3** keys at ``originals/versions/<version_id>/<filename>``
   from before PR #255 flattened the document model. Renamed to whatever the
   matching Document's ``original_object_key`` is (typically
   ``originals/docs/<doc_id>/<filename>``).

2. **Bug-era post-Stage-3** keys at bare ``versions/<doc_id>/<filename>``
   (no ``originals/`` prefix). PR #255 introduced a regression in
   ``api/routes/uploads.py`` that wrote new uploads to this third path
   instead of the spec-mandated ``originals/docs/<doc_id>/<filename>``.
   Fixed in PR #259; this pass migrates anything written during the gap.

Both passes are idempotent: walking each source prefix when nothing is
present is a no-op. Returns ``(renamed_count, orphans_deleted_count)``
combined across both phases.

Run at app startup; logs and skips cleanly if nothing remains under either
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


def _rename_legacy_originals_versions(session: "Session", backend, bucket: str) -> tuple[int, int, int]:
    """Phase 1: rename ``originals/versions/<version_id>/<f>`` (pre-Stage-3)
    keys to whatever the matching Document's ``original_object_key`` is.

    Returns ``(renamed, orphans_deleted, skipped_ambiguous)``.
    """
    # Map filename → [(doc_id_str, expected_new_key)] from the DB rows.
    # Multiple Documents could share a filename; if so we can't be sure
    # which old key matches which doc, so we skip those (logged).
    by_filename: dict[str, list[tuple[str, str]]] = defaultdict(list)
    docs = session.query(Document).filter(Document.original_object_key.isnot(None)).all()
    for doc in docs:
        if "/" not in doc.original_object_key:
            continue
        filename = doc.original_object_key.rsplit("/", 1)[-1]
        by_filename[filename].append((str(doc.doc_id), doc.original_object_key))

    renamed = 0
    orphans = 0
    skipped_ambiguous = 0

    for obj in backend.list_objects(bucket, prefix="originals/versions/", recursive=True):
        old_key = obj["key"]
        filename = old_key.rsplit("/", 1)[-1] if "/" in old_key else old_key

        candidates = by_filename.get(filename, [])
        if len(candidates) == 1:
            _doc_id, new_key = candidates[0]
            try:
                backend.copy_and_delete(bucket, old_key, bucket, new_key)
                renamed += 1
                logger.info("rename_originals[legacy]: %s -> %s", old_key, new_key)
            except Exception:
                logger.exception("rename_originals[legacy]: failed to move %s -> %s", old_key, new_key)
        elif len(candidates) > 1:
            logger.warning(
                "rename_originals[legacy]: ambiguous filename %s — %d candidates; skipping",
                filename,
                len(candidates),
            )
            skipped_ambiguous += 1
        else:
            try:
                backend.remove_object(bucket, old_key)
                orphans += 1
                logger.info("rename_originals[legacy]: orphan deleted %s", old_key)
            except Exception:
                logger.exception("rename_originals[legacy]: failed to delete orphan %s", old_key)

    return renamed, orphans, skipped_ambiguous


def _rename_bug_era_versions(session: "Session", backend, bucket: str) -> tuple[int, int]:
    """Phase 2: rename bare ``versions/<doc_id>/<f>`` (Stage-3 PR #255 bug-era)
    keys to spec-compliant ``originals/docs/<doc_id>/<f>``.

    Unlike the legacy phase, the bug-era keys directly encode the doc_id, so
    we don't need a filename-based lookup — we use the Document whose
    ``original_object_key`` matches the old key. Updates the Document row
    in place to point at the new key.

    Returns ``(renamed, orphans_deleted)``.
    """
    docs = session.query(Document).filter(Document.original_object_key.like("versions/%")).all()
    docs_by_key: dict[str, Document] = {doc.original_object_key: doc for doc in docs}

    renamed = 0
    orphans = 0
    touched = False

    for obj in backend.list_objects(bucket, prefix="versions/", recursive=True):
        old_key = obj["key"]
        doc = docs_by_key.get(old_key)
        if doc is not None:
            filename = old_key.rsplit("/", 1)[-1] if "/" in old_key else old_key
            new_key = f"originals/docs/{doc.doc_id}/{filename}"
            try:
                backend.copy_and_delete(bucket, old_key, bucket, new_key)
                doc.original_object_key = new_key
                touched = True
                renamed += 1
                logger.info("rename_originals[bug-era]: %s -> %s", old_key, new_key)
            except Exception:
                logger.exception("rename_originals[bug-era]: failed to move %s -> %s", old_key, new_key)
        else:
            try:
                backend.remove_object(bucket, old_key)
                orphans += 1
                logger.info("rename_originals[bug-era]: orphan deleted %s", old_key)
            except Exception:
                logger.exception("rename_originals[bug-era]: failed to delete orphan %s", old_key)

    if touched:
        session.commit()

    return renamed, orphans


def rename_all(session: "Session") -> tuple[int, int]:
    """Run both rename phases. Returns ``(renamed_total, orphans_total)``."""
    backend = get_storage()
    settings = get_settings()
    bucket = settings.minio_bucket  # same bucket name for both backends

    legacy_renamed, legacy_orphans, legacy_skipped = _rename_legacy_originals_versions(session, backend, bucket)
    bug_renamed, bug_orphans = _rename_bug_era_versions(session, backend, bucket)

    logger.info(
        "rename_originals: legacy phase: %d renamed, %d orphans deleted, %d skipped (ambiguous); "
        "bug-era phase: %d renamed, %d orphans deleted",
        legacy_renamed,
        legacy_orphans,
        legacy_skipped,
        bug_renamed,
        bug_orphans,
    )
    return legacy_renamed + bug_renamed, legacy_orphans + bug_orphans


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from harbor_clerk.db_sync import get_sync_session

    s = get_sync_session()
    try:
        rename_all(s)
    finally:
        s.close()
