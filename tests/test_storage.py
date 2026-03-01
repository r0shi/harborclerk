"""Tests for storage backends: FilesystemBackend and MinIOBackend."""

import io
from unittest.mock import MagicMock, patch

import pytest

from harbor_clerk.storage import FilesystemBackend, MinIOBackend, StorageResponse

# --- StorageResponse ---


def test_storage_response_read():
    resp = StorageResponse(b"hello world")
    assert resp.read() == b"hello world"


def test_storage_response_close_noop():
    resp = StorageResponse(b"data")
    resp.close()
    resp.release_conn()


# --- FilesystemBackend ---


@pytest.fixture
def fs_backend(tmp_path):
    with patch("harbor_clerk.storage.get_settings") as mock_settings:
        mock_settings.return_value.storage_path = str(tmp_path)
        backend = FilesystemBackend()
    return backend


def test_fs_ensure_bucket(fs_backend, tmp_path):
    fs_backend.ensure_bucket("test-bucket")
    assert (tmp_path / "test-bucket").is_dir()


def test_fs_bucket_exists(fs_backend, tmp_path):
    assert not fs_backend.bucket_exists("test-bucket")
    fs_backend.ensure_bucket("test-bucket")
    assert fs_backend.bucket_exists("test-bucket")


def test_fs_put_get_roundtrip(fs_backend):
    fs_backend.ensure_bucket("b")
    data = b"hello world"
    fs_backend.put_object("b", "path/to/file.txt", io.BytesIO(data), len(data))

    resp = fs_backend.get_object("b", "path/to/file.txt")
    assert resp.read() == data


def test_fs_get_nonexistent(fs_backend):
    fs_backend.ensure_bucket("b")
    with pytest.raises(FileNotFoundError):
        fs_backend.get_object("b", "no-such-file.txt")


def test_fs_remove_object(fs_backend):
    fs_backend.ensure_bucket("b")
    data = b"to delete"
    fs_backend.put_object("b", "del.txt", io.BytesIO(data), len(data))

    fs_backend.remove_object("b", "del.txt")
    with pytest.raises(FileNotFoundError):
        fs_backend.get_object("b", "del.txt")


def test_fs_remove_nonexistent_noop(fs_backend):
    fs_backend.ensure_bucket("b")
    fs_backend.remove_object("b", "nope.txt")  # should not raise


def test_fs_copy_object(fs_backend):
    fs_backend.ensure_bucket("b")
    data = b"copy me"
    fs_backend.put_object("b", "src.txt", io.BytesIO(data), len(data))

    fs_backend.copy_object("b", "dst.txt", "b", "src.txt")
    assert fs_backend.get_object("b", "dst.txt").read() == data
    assert fs_backend.get_object("b", "src.txt").read() == data  # original intact


def test_fs_copy_and_delete(fs_backend):
    fs_backend.ensure_bucket("b")
    data = b"move me"
    fs_backend.put_object("b", "old.txt", io.BytesIO(data), len(data))

    fs_backend.copy_and_delete("b", "old.txt", "b", "new.txt")
    assert fs_backend.get_object("b", "new.txt").read() == data
    with pytest.raises(FileNotFoundError):
        fs_backend.get_object("b", "old.txt")


def test_fs_list_objects_empty(fs_backend):
    fs_backend.ensure_bucket("b")
    assert fs_backend.list_objects("b") == []


def test_fs_list_objects_flat(fs_backend):
    fs_backend.ensure_bucket("b")
    for name in ["a.txt", "b.txt"]:
        fs_backend.put_object("b", name, io.BytesIO(b"x"), 1)

    objects = fs_backend.list_objects("b")
    keys = {o["key"] for o in objects}
    assert keys == {"a.txt", "b.txt"}


def test_fs_list_objects_recursive(fs_backend):
    fs_backend.ensure_bucket("b")
    fs_backend.put_object("b", "a.txt", io.BytesIO(b"x"), 1)
    fs_backend.put_object("b", "sub/b.txt", io.BytesIO(b"x"), 1)

    flat = fs_backend.list_objects("b", recursive=False)
    recursive = fs_backend.list_objects("b", recursive=True)

    flat_keys = {o["key"] for o in flat}
    recursive_keys = {o["key"] for o in recursive}

    assert "a.txt" in flat_keys
    assert "sub/b.txt" not in flat_keys  # nested file not in flat listing
    assert "a.txt" in recursive_keys
    assert "sub/b.txt" in recursive_keys


def test_fs_list_objects_with_prefix(fs_backend):
    fs_backend.ensure_bucket("b")
    fs_backend.put_object("b", "v1/a.txt", io.BytesIO(b"x"), 1)
    fs_backend.put_object("b", "v2/b.txt", io.BytesIO(b"x"), 1)

    result = fs_backend.list_objects("b", prefix="v1", recursive=True)
    keys = {o["key"] for o in result}
    assert keys == {"v1/a.txt"}


# --- MinIOBackend ---


@pytest.fixture
def minio_client():
    """Mock minio.Minio client."""
    return MagicMock()


@pytest.fixture
def minio_backend(minio_client):
    with patch("harbor_clerk.storage.get_settings") as mock_settings:
        mock_settings.return_value.minio_endpoint = "localhost:9000"
        mock_settings.return_value.minio_access_key = "minioadmin"
        mock_settings.return_value.minio_secret_key = "minioadmin123"
        mock_settings.return_value.minio_use_ssl = False
        mock_settings.return_value.minio_bucket = "originals"
        with patch("minio.Minio", return_value=minio_client):
            backend = MinIOBackend()
    return backend


def test_minio_put_object(minio_backend, minio_client):
    data = io.BytesIO(b"hello")
    minio_backend.put_object("originals", "key.txt", data, 5, "text/plain")
    minio_client.put_object.assert_called_once_with(
        "originals",
        "key.txt",
        data,
        5,
        content_type="text/plain",
    )


def test_minio_get_object(minio_backend, minio_client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"data"
    minio_client.get_object.return_value = mock_resp

    resp = minio_backend.get_object("originals", "key.txt")
    assert resp.read() == b"data"
    mock_resp.close.assert_called_once()
    mock_resp.release_conn.assert_called_once()


def test_minio_remove_object(minio_backend, minio_client):
    minio_backend.remove_object("originals", "key.txt")
    minio_client.remove_object.assert_called_once_with("originals", "key.txt")


def test_minio_copy_object(minio_backend, minio_client):
    minio_backend.copy_object("dst-bucket", "dst-key", "src-bucket", "src-key")
    minio_client.copy_object.assert_called_once()
    args = minio_client.copy_object.call_args
    assert args[0][0] == "dst-bucket"
    assert args[0][1] == "dst-key"


def test_minio_list_objects(minio_backend, minio_client):
    mock_obj = MagicMock()
    mock_obj.object_name = "file.txt"
    mock_obj.size = 42
    minio_client.list_objects.return_value = [mock_obj]

    result = minio_backend.list_objects("originals", prefix="v/", recursive=True)
    assert result == [{"key": "file.txt", "size": 42}]
    minio_client.list_objects.assert_called_once_with(
        "originals",
        prefix="v/",
        recursive=True,
    )


def test_minio_bucket_exists(minio_backend, minio_client):
    minio_client.bucket_exists.return_value = True
    assert minio_backend.bucket_exists("originals") is True


def test_minio_ensure_bucket_creates(minio_backend, minio_client):
    minio_client.bucket_exists.return_value = False
    minio_backend.ensure_bucket("originals")
    minio_client.make_bucket.assert_called_once_with("originals")


def test_minio_ensure_bucket_exists(minio_backend, minio_client):
    minio_client.bucket_exists.return_value = True
    minio_backend.ensure_bucket("originals")
    minio_client.make_bucket.assert_not_called()
