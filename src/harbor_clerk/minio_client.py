import logging

from minio import Minio
from minio.commonconfig import CopySource

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)


def get_minio_client() -> Minio:
    """Create a MinIO client."""
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )


def ensure_bucket_exists() -> None:
    """Create the originals bucket if it doesn't exist."""
    settings = get_settings()
    client = get_minio_client()
    bucket = settings.minio_bucket
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("Created MinIO bucket: %s", bucket)
    else:
        logger.info("MinIO bucket already exists: %s", bucket)


def copy_and_delete_object(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
) -> None:
    """Copy an object to a new key and delete the original."""
    client = get_minio_client()
    client.copy_object(
        dst_bucket,
        dst_key,
        CopySource(src_bucket, src_key),
    )
    client.remove_object(src_bucket, src_key)
