"""Storage backend abstraction — MinIO or local filesystem."""

import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)


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
        """Write an object only if nothing is at its path, atomically; raise FileExistsError otherwise.

        The bytes go to a temporary file in the destination directory and are
        fsynced, then hard-linked into place: the link fails if the path exists,
        so an existing file is never replaced, and a failed read leaves nothing
        at the final path. Where hard links are unsupported, the existence check
        and `os.replace` are separate steps.
        """
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as f:
                while chunk := data.read(64 * 1024):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise
            except OSError:
                if path.exists():
                    raise FileExistsError(str(path)) from None
                os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def bucket_is_empty(self, bucket: str) -> bool:
        """True if the bucket directory is missing or holds nothing. One directory read."""
        root = self._base / bucket
        if not root.is_dir():
            return True
        with os.scandir(root) as entries:
            return next(entries, None) is None

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
        if recursive:
            for path in search_root.rglob("*"):
                if path.is_file():
                    key = str(path.relative_to(bucket_root))
                    result.append({"key": key, "size": path.stat().st_size})
        else:
            for path in search_root.iterdir():
                if path.is_file():
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
            # The validator refuses this at startup; config.json overlays assign without validation.
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


async def log_missing_originals(session) -> bool:
    """Log an ERROR when documents have stored originals but the filesystem bucket is empty (#888).

    Catches an upgrade that started the filesystem-backed Compose stack without
    copying its originals out of MinIO; Compose always sets STORAGE_BACKEND, so
    the unset-variable warning in get_storage() never fires there. Cheap: one
    directory read and, only when that finds nothing, one EXISTS query. Returns
    whether it logged.
    """
    from sqlalchemy import exists, select

    from harbor_clerk.models import Document

    settings = get_settings()
    storage = get_storage()
    if not isinstance(storage, FilesystemBackend) or not storage.bucket_is_empty(settings.minio_bucket):
        return False
    referenced = await session.scalar(
        select(exists().where(Document.original_object_key.isnot(None), Document.status == "active"))
    )
    if not referenced:
        return False
    logger.error(
        "Documents reference stored originals (uploads or mail) but %s is empty, so downloading or "
        "re-extracting them will fail. If this install kept its originals in MinIO, copy them as "
        "docs/upgrade-runbook.md describes (section: Docker: MinIO to filesystem storage).",
        storage.location(settings.minio_bucket),
    )
    return True
