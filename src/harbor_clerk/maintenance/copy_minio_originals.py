"""Copy stored originals out of MinIO into the filesystem storage layout (#888).

Docker Compose stores originals on the filesystem backend now; the MinIO image
it used is no longer published. An install with uploads or IMAP mail in MinIO
runs this once, with `app` and `watcher` stopped and MinIO started from the
opt-in ``minio`` profile, before the new stack starts:

    docker compose run --rm --no-deps app python -m harbor_clerk.maintenance.copy_minio_originals

It lists the bucket (``MINIO_BUCKET``, default ``originals``) through the MinIO
backend and writes every object through the filesystem backend, which keeps the
same bucket and key: ``STORAGE_PATH/<bucket>/<key>``. Documents record the
bucket and key, so nothing in the database changes. MinIO is only read.

It never replaces a file that is already at the destination. Same size: the
object counts as already present. Different size: it is left alone, counted in
``differs`` and logged, because the filesystem copy may be newer (a new version
of an upload reuses its key) and MinIO's bytes must not replace it. Each object
is written to a temporary file, fsynced and linked into place, so an interrupted
run leaves no partial file and can simply be run again.

A zero-byte key ending in `/` is a folder marker, not an original, and is
skipped. A key with an empty, `.` or `..` segment between slashes (which covers
a leading `/`) is refused; a backslash is an ordinary character (Linux upload
keys keep one, e.g. `notes\\draft.txt`). Exits non-zero if any object failed.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harbor_clerk.storage import StorageBackend

if TYPE_CHECKING:
    from harbor_clerk.storage import FilesystemBackend

logger = logging.getLogger(__name__)


@dataclass
class CopyReport:
    copied: int = 0
    skipped: int = 0
    folder_markers: int = 0
    differs: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    partials_removed: int = 0
    listing_error: str | None = None


def _safe_key(key: str) -> bool:
    """A key the filesystem backend maps inside the bucket directory.

    No empty segment between slashes (which also refuses the empty key and a
    leading, doubled or trailing `/`), and no `.` or `..` segment. Only `/`
    separates segments; a backslash is an ordinary character.
    """
    return all(part not in ("", ".", "..") for part in key.split("/"))


def copy_originals(source: StorageBackend, destination: FilesystemBackend, bucket: str) -> CopyReport:
    """Copy every object in ``bucket`` from ``source`` to ``destination`` under the same key, never replacing a file."""
    report = CopyReport()
    report.partials_removed = destination.remove_partials(bucket)
    if report.partials_removed:
        logger.info("removed %d unfinished .partial-* file(s) left by an interrupted run", report.partials_removed)
    present = {obj["key"]: obj["size"] for obj in destination.list_objects(bucket, recursive=True)}
    destination.ensure_bucket(bucket)
    try:
        objects = source.list_objects(bucket, recursive=True)
    except Exception as exc:
        report.listing_error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "could not list bucket %r in MinIO (is it running and healthy, and are MINIO_* right?): %s",
            bucket,
            report.listing_error,
        )
        return report
    for obj in objects:
        key, size = obj["key"], obj["size"]
        if key.endswith("/") and size == 0:
            logger.info("skipping folder marker %r", key)
            report.folder_markers += 1
            continue
        if not _safe_key(key):
            logger.error("refusing key outside the bucket directory: %r", key)
            report.failed.append(key)
            continue
        if key in present:
            if present[key] == size:
                report.skipped += 1
            else:
                logger.warning(
                    "left alone, destination differs: %s (%d bytes in MinIO, %d here)", key, size, present[key]
                )
                report.differs.append(key)
            continue
        try:
            data = source.get_object(bucket, key).read()
            if len(data) != size:
                raise ValueError(f"read {len(data)} bytes, listed size {size}")
            destination.put_new_object(bucket, key, io.BytesIO(data))
        except FileExistsError:
            logger.warning("left alone, a file appeared at the destination after it was listed: %s", key)
            report.differs.append(key)
            continue
        except (IsADirectoryError, NotADirectoryError) as exc:
            logger.error("key collides with the destination's directory layout: %s (%s)", key, exc)
            report.failed.append(key)
            continue
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
        "copied %d, already present %d, differs %d, folder markers skipped %d, failed %d",
        report.copied,
        report.skipped,
        len(report.differs),
        report.folder_markers,
        len(report.failed),
    )
    return 1 if report.failed or report.listing_error else 0


if __name__ == "__main__":
    sys.exit(main())
