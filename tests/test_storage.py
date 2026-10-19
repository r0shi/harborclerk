"""Tests for storage backends: FilesystemBackend and MinIOBackend."""

import contextlib
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


# --- get_storage: default backend (#888) ---


@pytest.fixture
def fresh_storage(monkeypatch, tmp_path):
    """A clean singleton and a Settings built from the environment only (no .env file)."""
    import harbor_clerk.storage as storage_mod
    from harbor_clerk.config import Settings

    monkeypatch.setattr(storage_mod, "_storage", None)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

    def build(**env):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        settings = Settings(_env_file=None)
        monkeypatch.setattr(storage_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(storage_mod, "_storage", None)
        return storage_mod.get_storage()

    return build


def test_unset_backend_is_filesystem_and_warns(fresh_storage, tmp_path, caplog):
    """Unset used to mean MinIO at minio:9000, which resolves only inside Compose."""
    with caplog.at_level("INFO", logger="harbor_clerk.storage"):
        backend = fresh_storage()
    assert isinstance(backend, FilesystemBackend)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "STORAGE_BACKEND is not set" in warnings[0].getMessage()
    assert str(tmp_path.resolve()) in warnings[0].getMessage()


def test_explicit_filesystem_does_not_warn(fresh_storage, caplog):
    with caplog.at_level("INFO", logger="harbor_clerk.storage"):
        backend = fresh_storage(STORAGE_BACKEND="filesystem")
    assert isinstance(backend, FilesystemBackend)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_explicit_minio_still_selects_minio(fresh_storage, caplog):
    with patch("minio.Minio"), caplog.at_level("INFO", logger="harbor_clerk.storage"):
        backend = fresh_storage(STORAGE_BACKEND="minio")
    assert isinstance(backend, MinIOBackend)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --- FilesystemBackend.put_new_object: atomic, never replaces (#888) ---


class _FailsMidway(io.RawIOBase):
    """Yields one chunk, then raises: a read that fails part-way through."""

    def __init__(self):
        self.calls = 0

    def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return b"x" * 1024
        raise ConnectionError("source went away")


def _leftovers(root):
    return sorted(p.name for p in root.rglob("*") if p.is_file())


def test_put_new_object_writes_the_bytes(fs_backend, tmp_path):
    fs_backend.put_new_object("b", "d/k.bin", io.BytesIO(b"payload"))
    assert fs_backend.get_object("b", "d/k.bin").read() == b"payload"
    assert _leftovers(tmp_path) == ["k.bin"], "no temporary file is left behind"


def test_put_new_object_leaves_nothing_when_the_read_fails(fs_backend, tmp_path):
    with pytest.raises(ConnectionError):
        fs_backend.put_new_object("b", "d/k.bin", _FailsMidway())
    assert not (tmp_path / "b" / "d" / "k.bin").exists()
    assert _leftovers(tmp_path) == [], "neither a partial file nor a temporary one"


def test_put_new_object_never_replaces_an_existing_file(fs_backend, tmp_path):
    fs_backend.put_object("b", "k", io.BytesIO(b"newer"), 5)
    with pytest.raises(FileExistsError):
        fs_backend.put_new_object("b", "k", io.BytesIO(b"stale bytes"))
    assert fs_backend.get_object("b", "k").read() == b"newer"
    assert _leftovers(tmp_path) == ["k"]


def test_put_new_object_without_hard_links(fs_backend, tmp_path):
    """Where link() is unsupported the write still lands, and still refuses an existing file."""
    with patch("harbor_clerk.storage.os.link", side_effect=PermissionError("no hard links here")):
        fs_backend.put_new_object("b", "k", io.BytesIO(b"first"))
        with pytest.raises(FileExistsError):
            fs_backend.put_new_object("b", "k", io.BytesIO(b"second"))
    assert fs_backend.get_object("b", "k").read() == b"first"
    assert _leftovers(tmp_path) == ["k"]


def test_put_new_object_gives_put_objects_mode(fs_backend, tmp_path):
    """mkstemp creates 0600 and the hard link keeps it; a copied original must be readable like any other."""
    fs_backend.put_new_object("b", "k", io.BytesIO(b"x"))
    assert (tmp_path / "b" / "k").stat().st_mode & 0o777 == 0o644


def test_put_new_object_refuses_a_key_whose_parent_is_a_file(fs_backend, tmp_path):
    fs_backend.put_object("b", "a", io.BytesIO(b"file"), 4)
    with pytest.raises(NotADirectoryError, match="a parent of 'a/b' is a file"):
        fs_backend.put_new_object("b", "a/b", io.BytesIO(b"x"))
    with pytest.raises(NotADirectoryError):
        fs_backend.put_new_object("b", "a/b/c", io.BytesIO(b"x"))
    assert fs_backend.get_object("b", "a").read() == b"file"
    assert _leftovers(tmp_path) == ["a"]


@pytest.mark.parametrize("hard_links", [True, False])
def test_put_new_object_refuses_a_key_that_is_a_directory(fs_backend, tmp_path, hard_links):
    fs_backend.put_object("b", "a/b", io.BytesIO(b"inner"), 5)
    link = (
        patch("harbor_clerk.storage.os.link", side_effect=PermissionError("no hard links")) if not hard_links else None
    )
    with link or contextlib.nullcontext(), pytest.raises(IsADirectoryError, match="'a' is a directory"):
        fs_backend.put_new_object("b", "a", io.BytesIO(b"x"))
    assert _leftovers(tmp_path) == ["b"], "only a/b, no temporary file"


def test_list_objects_skips_partial_files(fs_backend, tmp_path):
    fs_backend.put_object("b", "d/real", io.BytesIO(b"r"), 1)
    (tmp_path / "b" / "d" / ".partial-abc123").write_bytes(b"half")
    (tmp_path / "b" / ".partial-top").write_bytes(b"half")
    assert [o["key"] for o in fs_backend.list_objects("b", recursive=True)] == ["d/real"]
    assert fs_backend.list_objects("b") == []
    assert [o["key"] for o in fs_backend.list_objects("b", prefix="d")] == ["d/real"]


def test_remove_partials(fs_backend, tmp_path):
    assert fs_backend.remove_partials("missing") == 0
    fs_backend.put_object("b", "d/real", io.BytesIO(b"r"), 1)
    (tmp_path / "b" / "d" / ".partial-1").write_bytes(b"half")
    (tmp_path / "b" / ".partial-2").write_bytes(b"half")
    assert fs_backend.remove_partials("b") == 2
    assert _leftovers(tmp_path) == ["real"]


def test_object_exists(fs_backend):
    assert not fs_backend.object_exists("b", "d/k")
    fs_backend.put_object("b", "d/k", io.BytesIO(b"k"), 1)
    assert fs_backend.object_exists("b", "d/k")
    assert not fs_backend.object_exists("b", "d"), "a directory is not an object"


# --- STORAGE_BACKEND parsing (#888) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("filesystem", "filesystem"), (" Filesystem ", "filesystem"), ("MINIO", "minio"), ("", "filesystem")],
)
def test_storage_backend_is_normalised(monkeypatch, raw, expected):
    from harbor_clerk.config import Settings

    monkeypatch.setenv("STORAGE_BACKEND", raw)
    assert Settings(_env_file=None).storage_backend == expected


def test_unknown_storage_backend_fails_at_startup(monkeypatch):
    from pydantic import ValidationError

    from harbor_clerk.config import Settings

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    with pytest.raises(ValidationError, match="STORAGE_BACKEND must be one of filesystem, minio; got 's3'"):
        Settings(_env_file=None)


def test_config_json_overlays_are_validated_too(monkeypatch):
    """config.py's _apply_to assigns through validate_assignment, so the validator covers overlays."""
    from pydantic import ValidationError

    from harbor_clerk.config import Settings

    settings = Settings(_env_file=None)
    validator = type(settings).__pydantic_validator__
    validator.validate_assignment(settings, "storage_backend", " MinIO ")
    assert settings.storage_backend == "minio"
    with pytest.raises(ValidationError, match="STORAGE_BACKEND must be one of"):
        validator.validate_assignment(settings, "storage_backend", "s3")


def test_get_storage_refuses_an_unvalidated_unknown_backend(fresh_storage, monkeypatch):
    """Defensive: only reachable if something bypasses validation; it must not fall back to MinIO."""
    import harbor_clerk.storage as storage_mod
    from harbor_clerk.config import Settings

    settings = Settings(_env_file=None)
    object.__setattr__(settings, "storage_backend", "Filesystem")
    monkeypatch.setattr(storage_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_mod, "_storage", None)
    with pytest.raises(ValueError, match="filesystem or minio"):
        storage_mod.get_storage()
