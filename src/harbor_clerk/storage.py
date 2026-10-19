"""Storage backend abstraction — MinIO or local filesystem."""

import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, NoReturn

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)

# Temporary files of FilesystemBackend.put_new_object; never listed as objects.
PARTIAL_PREFIX = ".partial-"


class StorageResponse:
    """Wrapper providing a consistent interface for retrieved objects."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class StorageBackend(ABC):
    """Abstract storage backend."""

    @abstractmethod
    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        content_type: str = "",
    ) -> None: ...

    @abstractmethod
    def get_object(self, bucket: str, key: str) -> StorageResponse: ...

    @abstractmethod
    def copy_object(
        self,
        dst_bucket: str,
        dst_key: str,
        src_bucket: str,
        src_key: str,
    ) -> None: ...

    @abstractmethod
    def remove_object(self, bucket: str, key: str) -> None: ...

    @abstractmethod
    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        recursive: bool = False,
    ) -> list[dict]: ...

    @abstractmethod
    def bucket_exists(self, bucket: str) -> bool: ...

    @abstractmethod
    def ensure_bucket(self, bucket: str) -> None: ...

    def copy_and_delete(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> None:
        """Copy object to new key, then delete the original."""
        self.copy_object(dst_bucket, dst_key, src_bucket, src_key)
        self.remove_object(src_bucket, src_key)


class MinIOBackend(StorageBackend):
    """MinIO / S3-compatible object storage."""

    def __init__(self):
        from minio import Minio

        settings = get_settings()
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_use_ssl,
        )
        self._bucket = settings.minio_bucket

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        content_type: str = "",
    ) -> None:
        self._client.put_object(bucket, key, data, length, content_type=content_type)

    def get_object(self, bucket: str, key: str) -> StorageResponse:
        response = self._client.get_object(bucket, key)
        data = response.read()
        response.close()
        response.release_conn()
        return StorageResponse(data)

    def copy_object(
        self,
        dst_bucket: str,
        dst_key: str,
        src_bucket: str,
        src_key: str,
    ) -> None:
        from minio.commonconfig import CopySource

        self._client.copy_object(dst_bucket, dst_key, CopySource(src_bucket, src_key))

    def remove_object(self, bucket: str, key: str) -> None:
        self._client.remove_object(bucket, key)

    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        recursive: bool = False,
    ) -> list[dict]:
        result = []
        for obj in self._client.list_objects(bucket, prefix=prefix, recursive=recursive):
            result.append({"key": obj.object_name, "size": obj.size or 0})
        return result

    def bucket_exists(self, bucket: str) -> bool:
        return self._client.bucket_exists(bucket)

    def ensure_bucket(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)
            logger.info("Created MinIO bucket: %s", bucket)
        else:
            logger.info("MinIO bucket already exists: %s", bucket)


class FilesystemBackend(StorageBackend):
    """Local filesystem storage, same key structure as MinIO."""

    def __init__(self):
        settings = get_settings()
        self._base = Path(settings.storage_path).expanduser()

    def _path(self, bucket: str, key: str) -> Path:
        return self._base / bucket / key

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        content_type: str = "",
    ) -> None:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            while True:
                chunk = data.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    def put_new_object(self, bucket: str, key: str, data: BinaryIO) -> None:
        """Write an object only if nothing is at its path, atomically.

        The bytes go to a temporary file (`.partial-*`) in the destination
        directory, are fsynced and given put_object's 0644, then hard-linked into
        place: the link fails if the path exists, so an existing file is never
        replaced, and a failed read leaves nothing at the final path. Where hard
        links are unsupported, the existence check and `os.replace` are separate
        steps.

        Raises FileExistsError if a regular file is already at the key,
        IsADirectoryError if a directory is (key `a` against existing `a/b`), and
        NotADirectoryError if a parent of the key is a file (`a/b` against `a`).
        """
        path = self._path(bucket, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            raise NotADirectoryError(f"a parent of {key!r} is a file, not a directory") from None
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=PARTIAL_PREFIX)
        try:
            with os.fdopen(fd, "wb") as f:
                while chunk := data.read(64 * 1024):
                    f.write(chunk)
                f.flush()
                os.fchmod(f.fileno(), 0o644)
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                self._raise_existing(path, key)
            except OSError:
                if path.exists():
                    self._raise_existing(path, key)
                os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _raise_existing(path: Path, key: str) -> NoReturn:
        if path.is_dir() and not path.is_symlink():
            raise IsADirectoryError(f"{key!r} is a directory, not a file")
        raise FileExistsError(str(path))

    def remove_partials(self, bucket: str) -> int:
        """Delete `.partial-*` files an interrupted put_new_object left behind. Returns how many."""
        root = self._base / bucket
        if not root.is_dir():
            return 0
        removed = 0
        for path in root.rglob(PARTIAL_PREFIX + "*"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def object_exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).is_file()

    def location(self, bucket: str) -> str:
        return str((self._base / bucket).resolve())

    def get_object(self, bucket: str, key: str) -> StorageResponse:
        path = self._path(bucket, key)
        if not path.is_file():
            raise FileNotFoundError(f"Object not found: {bucket}/{key}")
        return StorageResponse(path.read_bytes())

    def copy_object(
        self,
        dst_bucket: str,
        dst_key: str,
        src_bucket: str,
        src_key: str,
    ) -> None:
        src = self._path(src_bucket, src_key)
        dst = self._path(dst_bucket, dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def remove_object(self, bucket: str, key: str) -> None:
        path = self._path(bucket, key)
        if path.is_file():
            path.unlink()
            # Clean up empty parent dirs up to bucket root
            bucket_root = self._base / bucket
            parent = path.parent
            while parent != bucket_root:
                try:
                    parent.rmdir()  # only succeeds if empty
                except OSError:
                    break
                parent = parent.parent

    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        recursive: bool = False,
    ) -> list[dict]:
        bucket_root = self._base / bucket
        search_root = bucket_root / prefix if prefix else bucket_root
        if not search_root.exists():
            return []

        result = []
        paths = search_root.rglob("*") if recursive else search_root.iterdir()
        for path in paths:
            # A `.partial-*` file is an unfinished put_new_object, never an object.
            if path.is_file() and not path.name.startswith(PARTIAL_PREFIX):
                key = str(path.relative_to(bucket_root))
                result.append({"key": key, "size": path.stat().st_size})
        return result

    def bucket_exists(self, bucket: str) -> bool:
        return (self._base / bucket).is_dir()

    def ensure_bucket(self, bucket: str) -> None:
        path = self._base / bucket
        path.mkdir(parents=True, exist_ok=True)
        logger.info("Ensured storage directory: %s", path)


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Get the configured storage backend (singleton)."""
    global _storage
    if _storage is None:
        settings = get_settings()
        if settings.storage_backend == "filesystem":
            _storage = FilesystemBackend()
            location = str(Path(settings.storage_path).expanduser().resolve())
        elif settings.storage_backend == "minio":
            _storage = MinIOBackend()
            location = settings.minio_endpoint
        else:
            # Defensive: the validator normalises every value, from the environment and from
            # config.json overlays alike (they assign through validate_assignment), so this is
            # reached only if something bypasses validation.
            raise ValueError(f"STORAGE_BACKEND must be filesystem or minio; got {settings.storage_backend!r}")
        if "storage_backend" in settings.model_fields_set:
            logger.info("Storage backend: %s (%s)", settings.storage_backend, location)
        else:
            # The default was "minio" before #888. A deployment that relied on it
            # now stores originals here instead; say so where an operator looks.
            logger.warning(
                "STORAGE_BACKEND is not set: storing originals on the filesystem at %s. "
                "An install whose originals are in MinIO must set STORAGE_BACKEND=minio.",
                location,
            )
    return _storage


MISSING_ORIGINALS_SAMPLE = 5


async def log_missing_originals(session) -> bool:
    """Log an ERROR when sampled stored originals are missing from the filesystem backend (#888).

    Catches an upgrade that started the filesystem-backed Compose stack without
    copying its originals out of MinIO. Compose always sets STORAGE_BACKEND, so
    the unset-variable warning in get_storage() never fires there, and the
    bucket is rarely empty by the time this runs: the watcher can ingest mail
    before `app` (which waits on the embedder and reranker) starts. So it samples
    the oldest active documents with a stored original, the ones an upgrade would
    have left in MinIO, and checks that their files exist. Cheap: one query and at
    most MISSING_ORIGINALS_SAMPLE stats. Returns whether it logged.
    """
    from sqlalchemy import select

    from harbor_clerk.models import Document

    settings = get_settings()
    storage = get_storage()
    if not isinstance(storage, FilesystemBackend):
        return False
    rows = (
        await session.execute(
            select(Document.original_bucket, Document.original_object_key)
            .where(Document.original_object_key.isnot(None), Document.status == "active")
            .order_by(Document.created_at)
            .limit(MISSING_ORIGINALS_SAMPLE)
        )
    ).all()
    missing = [key for bucket, key in rows if not storage.object_exists(bucket or settings.minio_bucket, key)]
    if not missing:
        return False
    logger.error(
        "%d of the %d oldest documents with a stored original (upload or mail) have no file under %s, so "
        "downloading or re-extracting them will fail. If this install kept its originals in MinIO, copy them "
        "as docs/upgrade-runbook.md describes (section: Docker: MinIO to filesystem storage).",
        len(missing),
        len(rows),
        storage.location(settings.minio_bucket),
    )
    return True
