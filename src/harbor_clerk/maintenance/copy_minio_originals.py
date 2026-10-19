"""Copy stored originals out of MinIO into the filesystem storage layout (#888).

Docker Compose stores originals on the filesystem backend now; the MinIO image
it used is no longer published. An install with uploads or IMAP mail in MinIO
runs this once, with MinIO started from the opt-in ``minio`` profile:

    docker compose run --rm --no-deps app python -m harbor_clerk.maintenance.copy_minio_originals

It lists the bucket (``MINIO_BUCKET``, default ``originals``) through the MinIO
backend and writes every object through the filesystem backend, which keeps the
same bucket and key: ``STORAGE_PATH/<bucket>/<key>``. Documents record the
bucket and key, so nothing in the database changes. MinIO is only read.

An object already present at the destination with the same size is skipped, so
an interrupted run is resumed by running it again (a partly written file has a
different size and is copied again). A zero-byte key ending in `/` is a folder
marker, not an original, and is skipped. A key that could resolve outside the
bucket directory (leading `/`, an empty, `.` or `..` segment) is refused; a
backslash is an ordinary character (an upload named `notes\\draft.txt` on Linux
keeps it), but `..` between backslashes is still refused. Exits non-zero if any
object was not copied.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field

from harbor_clerk.storage import StorageBackend

logger = logging.getLogger(__name__)


@dataclass
class CopyReport:
    copied: int = 0
    skipped: int = 0
    folder_markers: int = 0
    failed: list[str] = field(default_factory=list)


def _safe_key(key: str) -> bool:
    """A key the filesystem backend maps inside the bucket directory.

    No empty segment, which also refuses the empty key and a leading, doubled or
    trailing `/`; and no `.` or `..` segment whether split on `/` or on a
    backslash. A backslash inside a name is otherwise allowed.
    """
    parts = key.split("/")
    if "" in parts:
        return False
    return not any(seg in (".", "..") for part in parts for seg in part.split("\\"))


def copy_originals(source: StorageBackend, destination: StorageBackend, bucket: str) -> CopyReport:
    """Copy every object in ``bucket`` from ``source`` to ``destination`` under the same key."""
    report = CopyReport()
    present = {obj["key"]: obj["size"] for obj in destination.list_objects(bucket, recursive=True)}
    destination.ensure_bucket(bucket)
    for obj in source.list_objects(bucket, recursive=True):
        key, size = obj["key"], obj["size"]
        if key.endswith("/") and size == 0:
            logger.info("skipping folder marker %r", key)
            report.folder_markers += 1
            continue
        if not _safe_key(key):
            logger.error("refusing key outside the bucket directory: %r", key)
            report.failed.append(key)
            continue
        if present.get(key) == size:
            report.skipped += 1
            continue
        try:
            data = source.get_object(bucket, key).read()
            if len(data) != size:
                raise ValueError(f"read {len(data)} bytes, listed size {size}")
            destination.put_object(bucket, key, io.BytesIO(data), len(data))
        except Exception:
            logger.exception("failed to copy %s/%s", bucket, key)
            report.failed.append(key)
            continue
        report.copied += 1
    return report


def main(argv: list[str] | None = None) -> int:
    from harbor_clerk.config import get_settings
    from harbor_clerk.storage import FilesystemBackend, MinIOBackend

    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", default=settings.minio_bucket, help="bucket to copy (default: MINIO_BUCKET)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    logger.info("copying bucket %r from MinIO at %s to %s", args.bucket, settings.minio_endpoint, settings.storage_path)
    report = copy_originals(MinIOBackend(), FilesystemBackend(), args.bucket)
    logger.info(
        "copied %d, already present %d, folder markers skipped %d, failed %d",
        report.copied,
        report.skipped,
        report.folder_markers,
        len(report.failed),
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
