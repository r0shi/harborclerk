"""The MinIO-to-filesystem copy that moves a Docker install off the dead MinIO image (#888)."""

from unittest.mock import patch

import pytest

from harbor_clerk.maintenance import copy_minio_originals as mod
from harbor_clerk.maintenance.copy_minio_originals import _safe_key, copy_originals
from harbor_clerk.storage import FilesystemBackend, StorageBackend, StorageResponse

BUCKET = "originals"


class FakeMinIO(StorageBackend):
    """An in-memory source; `listed_size` lets a test list a size the read disagrees with."""

    def __init__(self, objects: dict[str, bytes], listed_size: dict[str, int] | None = None, broken=()):
        self.objects = objects
        self.listed_size = listed_size or {}
        self.broken = set(broken)
        self.writes = 0

    def list_objects(self, bucket, prefix="", recursive=False):
        assert bucket == BUCKET and recursive
        return [{"key": k, "size": self.listed_size.get(k, len(v))} for k, v in self.objects.items()]

    def get_object(self, bucket, key):
        if key in self.broken:
            raise ConnectionError("minio went away")
        return StorageResponse(self.objects[key])

    def put_object(self, *args, **kwargs):
        self.writes += 1

    def copy_object(self, *args, **kwargs):
        self.writes += 1

    def remove_object(self, *args, **kwargs):
        self.writes += 1

    def bucket_exists(self, bucket):
        return True

    def ensure_bucket(self, bucket):
        self.writes += 1


@pytest.fixture
def dest(tmp_path):
    with patch("harbor_clerk.storage.get_settings") as settings:
        settings.return_value.storage_path = str(tmp_path)
        return FilesystemBackend()


def test_copies_every_object_to_bucket_slash_key(dest, tmp_path):
    source = FakeMinIO({"originals/docs/1/a.pdf": b"pdf bytes", "mail/2/msg.eml": b"From: x"})
    report = copy_originals(source, dest, BUCKET)
    assert (report.copied, report.skipped, report.failed) == (2, 0, [])
    # The layout FilesystemBackend reads: STORAGE_PATH/<bucket>/<key>.
    assert (tmp_path / BUCKET / "originals/docs/1/a.pdf").read_bytes() == b"pdf bytes"
    assert dest.get_object(BUCKET, "mail/2/msg.eml").read() == b"From: x"
    assert source.writes == 0, "MinIO must only be read"


def test_rerun_skips_what_is_already_copied(dest):
    source = FakeMinIO({"a": b"complete", "b": b"also complete"})
    copy_originals(source, dest, BUCKET)
    report = copy_originals(source, dest, BUCKET)
    assert (report.copied, report.skipped, report.differs, report.failed) == (0, 2, [], [])


def test_a_different_destination_file_is_never_replaced(dest, tmp_path):
    """A new version of an upload reuses its key; MinIO's stale bytes must not replace it."""
    key = "originals/docs/1111/report.pdf"
    (tmp_path / BUCKET / "originals/docs/1111").mkdir(parents=True)
    (tmp_path / BUCKET / key).write_bytes(b"NEW version 2 bytes")
    report = copy_originals(FakeMinIO({key: b"OLD v1", "other": b"o"}), dest, BUCKET)
    assert (report.copied, report.skipped, report.differs, report.failed) == (1, 0, [key], [])
    assert (tmp_path / BUCKET / key).read_bytes() == b"NEW version 2 bytes"


def test_a_file_appearing_during_the_copy_is_left_alone(dest, tmp_path):
    """Listed absent, present by write time: put_new_object refuses, and the file survives."""

    class Racing(FakeMinIO):
        def get_object(self, bucket, key):
            (tmp_path / BUCKET / key).write_bytes(b"written meanwhile")
            return super().get_object(bucket, key)

    report = copy_originals(Racing({"k": b"from minio"}), dest, BUCKET)
    assert (report.copied, report.differs, report.failed) == (0, ["k"], [])
    assert (tmp_path / BUCKET / "k").read_bytes() == b"written meanwhile"


class SpyDest(FilesystemBackend):
    """Records every key it is asked to write, so a refusal is proven before any write is attempted."""

    def __init__(self):
        super().__init__()
        self.written: list[str] = []

    def put_new_object(self, bucket, key, data):
        self.written.append(key)
        super().put_new_object(bucket, key, data)


@pytest.fixture
def spy(tmp_path):
    with patch("harbor_clerk.storage.get_settings") as settings:
        settings.return_value.storage_path = str(tmp_path)
        return SpyDest()


REFUSED = ["../escape", "a/../../escape", "/abs", "a//b", "./a", "a/.", "a/", ""]


@pytest.mark.parametrize("key", REFUSED)
def test_a_key_outside_the_bucket_is_refused_before_any_write(spy, tmp_path, key):
    assert _safe_key(key) is False
    report = copy_originals(FakeMinIO({key: b"x", "ok": b"y"}), spy, BUCKET)
    assert report.failed == [key]
    assert spy.written == ["ok"], "a refused key must never reach the destination"
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["ok"]


@pytest.mark.parametrize("name", ["notes\\draft.txt", "..\\x.pdf", "a\\..\\b", ".\\y"])
def test_a_backslash_is_an_ordinary_character(dest, tmp_path, name):
    """Upload keys keep a backslash on Linux (`os.path.basename("notes\\draft.txt")`): one component."""
    key = f"originals/docs/1/{name}"
    assert _safe_key(key) is True
    report = copy_originals(FakeMinIO({key: b"draft"}), dest, BUCKET)
    assert (report.copied, report.failed) == (1, [])
    assert dest.get_object(BUCKET, key).read() == b"draft"
    assert (tmp_path / BUCKET / "originals/docs/1" / name).read_bytes() == b"draft"


def test_a_folder_marker_is_skipped_not_failed(spy):
    report = copy_originals(FakeMinIO({"originals/docs/": b"", "originals/docs/1/a": b"a"}), spy, BUCKET)
    assert (report.copied, report.folder_markers, report.failed) == (1, 1, [])
    assert spy.written == ["originals/docs/1/a"]


def test_a_non_empty_key_ending_in_slash_is_refused(spy):
    report = copy_originals(FakeMinIO({"odd/": b"data"}), spy, BUCKET)
    assert (report.folder_markers, report.failed, spy.written) == (0, ["odd/"], [])


def test_a_short_read_is_not_written(dest, tmp_path):
    report = copy_originals(FakeMinIO({"a": b"short"}, listed_size={"a": 99}), dest, BUCKET)
    assert report.failed == ["a"]
    assert not (tmp_path / BUCKET / "a").exists()


def test_a_failed_read_does_not_stop_the_rest(dest):
    report = copy_originals(FakeMinIO({"bad": b"x", "good": b"y"}, broken={"bad"}), dest, BUCKET)
    assert report.failed == ["bad"]
    assert report.copied == 1, "a failed object is not counted as copied"
    assert dest.get_object(BUCKET, "good").read() == b"y"


def test_a_refused_key_is_not_counted_as_copied(spy):
    report = copy_originals(FakeMinIO({"../x": b"x", "ok": b"y"}), spy, BUCKET)
    assert (report.copied, report.failed) == (1, ["../x"])


def test_main_exit_code_reports_failures(dest):
    def run(source):
        with (
            patch("harbor_clerk.storage.MinIOBackend", return_value=source),
            patch("harbor_clerk.storage.FilesystemBackend", return_value=dest),
        ):
            return mod.main([])

    assert run(FakeMinIO({"a": b"x"})) == 0
    assert run(FakeMinIO({"b": b"x"}, broken={"b"})) == 1


def test_bucket_flag_selects_the_bucket(dest):
    seen = []

    class Recording(FakeMinIO):
        def list_objects(self, bucket, prefix="", recursive=False):
            seen.append(bucket)
            return []

    with (
        patch("harbor_clerk.storage.MinIOBackend", return_value=Recording({})),
        patch("harbor_clerk.storage.FilesystemBackend", return_value=dest),
    ):
        assert mod.main(["--bucket", "custom"]) == 0
    assert seen == ["custom"]


def test_an_object_larger_than_the_write_chunk_arrives_whole(dest):
    payload = bytes(range(256)) * 1000  # FilesystemBackend writes 64 KiB at a time
    copy_originals(FakeMinIO({"big.bin": payload}), dest, BUCKET)
    assert dest.get_object(BUCKET, "big.bin").read() == payload


def test_partial_files_from_an_interrupted_run_are_swept_first(dest, tmp_path, caplog):
    """A kill mid-copy leaves `.partial-*` files; the next run removes them and still copies everything."""
    (tmp_path / BUCKET / "originals/docs/1").mkdir(parents=True)
    (tmp_path / BUCKET / "originals/docs/1/.partial-x1").write_bytes(b"half")
    (tmp_path / BUCKET / ".partial-x2").write_bytes(b"half")
    with caplog.at_level("INFO", logger="harbor_clerk.maintenance.copy_minio_originals"):
        report = copy_originals(FakeMinIO({"originals/docs/1/a.pdf": b"a"}), dest, BUCKET)
    assert (report.partials_removed, report.copied, report.failed) == (2, 1, [])
    assert any("removed 2 unfinished" in r.getMessage() for r in caplog.records)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["a.pdf"]


@pytest.mark.parametrize("existing", ["file", "directory"])
def test_a_file_directory_collision_fails_rather_than_differs(dest, tmp_path, existing, caplog):
    """`a/b` against an existing file `a`, and `a` against an existing `a/b`: a layout clash, not a newer file."""
    if existing == "file":
        (tmp_path / BUCKET).mkdir()
        (tmp_path / BUCKET / "a").write_bytes(b"file a")
        key = "a/b"
    else:
        (tmp_path / BUCKET / "a").mkdir(parents=True)
        (tmp_path / BUCKET / "a" / "b").write_bytes(b"file a/b")
        key = "a"
    with caplog.at_level("ERROR", logger="harbor_clerk.maintenance.copy_minio_originals"):
        report = copy_originals(FakeMinIO({key: b"from minio"}), dest, BUCKET)
    assert (report.copied, report.differs, report.failed) == (0, [], [key])
    assert any("collides with the destination's directory layout" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("order", [("a", "a/b"), ("a/b", "a")])
def test_a_collision_within_minio_fails_the_second_key(dest, order):
    report = copy_originals(FakeMinIO({k: k.encode() for k in order}), dest, BUCKET)
    assert (report.copied, report.differs, report.failed) == (1, [], [order[1]])


def test_an_unreachable_minio_is_a_clear_error_not_a_traceback(dest, caplog):
    class Down(FakeMinIO):
        def list_objects(self, bucket, prefix="", recursive=False):
            raise ConnectionRefusedError("[Errno 111] Connection refused")

    with (
        patch("harbor_clerk.storage.MinIOBackend", return_value=Down({})),
        patch("harbor_clerk.storage.FilesystemBackend", return_value=dest),
        caplog.at_level("ERROR", logger="harbor_clerk.maintenance.copy_minio_originals"),
    ):
        assert mod.main([]) == 1
    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "could not list bucket 'originals' in MinIO" in record.getMessage()
    assert "Connection refused" in record.getMessage()
    assert record.exc_info is None, "a one-line error, not a traceback"
