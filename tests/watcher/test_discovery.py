import logging

from harbor_clerk.models.watched import WatchedFolder
from harbor_clerk.watcher.discovery import log_watch_root_problem, scan_watch_root, watch_root_problem


def test_new_subdir_creates_auto_discovered_folder(sync_session, tmp_path):
    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    rows = sync_session.query(WatchedFolder).all()
    assert len(rows) == 1
    assert rows[0].path == str(tmp_path / "inbox")
    assert rows[0].auto_discovered is True
    assert rows[0].display_name == "inbox"
    assert rows[0].unavailable_reason is None


def test_missing_subdir_marks_unavailable(sync_session, tmp_path):
    (tmp_path / "contracts").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    (tmp_path / "contracts").rmdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    row = sync_session.query(WatchedFolder).one()
    assert row.unavailable_reason == "unmounted"
    assert row.enabled is False


def test_remounted_subdir_clears_unavailable(sync_session, tmp_path):
    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    (tmp_path / "inbox").rmdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    row = sync_session.query(WatchedFolder).one()
    assert row.unavailable_reason is None
    assert row.enabled is True


def test_files_at_root_ignored(sync_session, tmp_path):
    (tmp_path / "loose.txt").write_text("hi")
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    assert sync_session.query(WatchedFolder).count() == 0


def test_empty_watch_root_string_is_noop(sync_session):
    scan_watch_root(sync_session, "")  # must not raise
    assert sync_session.query(WatchedFolder).count() == 0


def test_nonexistent_watch_root_is_noop(sync_session, tmp_path):
    scan_watch_root(sync_session, str(tmp_path / "does-not-exist"))
    assert sync_session.query(WatchedFolder).count() == 0


def test_manually_added_folders_are_not_marked_unmounted(sync_session, tmp_path):
    """auto_discovered=False rows are owned by the API, not by the discovery loop."""
    manual = WatchedFolder(
        path="/some/manual/path",
        display_name="manual",
        bookmark_data=None,
        auto_discovered=False,
    )
    sync_session.add(manual)
    sync_session.commit()

    scan_watch_root(sync_session, str(tmp_path))  # doesn't include /some/manual/path
    sync_session.commit()

    row = sync_session.query(WatchedFolder).one()
    assert row.auto_discovered is False
    assert row.unavailable_reason is None  # untouched by discovery
    assert row.enabled is True


def test_manual_row_for_the_path_is_not_duplicated(sync_session, tmp_path):
    """A folder added through the API (auto_discovered=False) is not discovered a second time.

    The #870 review's scenario: on Compose, POST /api/watch/folders accepted a
    subdirectory of WATCH_ROOT, then the discovery loop inserted a second row for
    the same path, so every file was observed and ingested twice.
    """
    (tmp_path / "inbox").mkdir()
    sync_session.add(
        WatchedFolder(path=str(tmp_path / "inbox"), auto_discovered=False, display_name="inbox", bookmark_data=None)
    )
    sync_session.commit()

    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    rows = sync_session.query(WatchedFolder).all()
    assert [(r.path, r.auto_discovered) for r in rows] == [(str(tmp_path / "inbox"), False)]


def test_known_path_is_matched_after_resolving_symlinks(sync_session, tmp_path):
    real = tmp_path / "real"
    (real / "inbox").mkdir(parents=True)
    (tmp_path / "root").symlink_to(real)
    sync_session.add(
        WatchedFolder(path=str(real / "inbox"), auto_discovered=False, display_name="inbox", bookmark_data=None)
    )
    sync_session.commit()

    scan_watch_root(sync_session, str(tmp_path / "root"))
    sync_session.commit()
    assert sync_session.query(WatchedFolder).count() == 1


def _mountinfo(tmp_path, *mount_points: str):
    lines = [f"{i} 1 0:{i} / {mp} rw,relatime - ext4 /dev/x rw" for i, mp in enumerate(mount_points, start=20)]
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_watch_root_problem_empty_root_is_fine(tmp_path):
    assert watch_root_problem("", _mountinfo(tmp_path, "/")) is None


def test_watch_root_problem_missing_root(tmp_path):
    problem = watch_root_problem(str(tmp_path / "library"), _mountinfo(tmp_path, "/", str(tmp_path)))
    assert problem is not None and "does not exist" in problem


def test_watch_root_problem_relative_root(tmp_path):
    problem = watch_root_problem("data/watch", _mountinfo(tmp_path, "/"))
    assert problem is not None and "absolute" in problem


def test_watch_root_problem_root_is_the_mount(tmp_path):
    root = tmp_path / "watch"
    root.mkdir()
    assert watch_root_problem(str(root), _mountinfo(tmp_path, "/", str(root))) is None


def test_watch_root_problem_root_under_the_mount(tmp_path):
    """WATCH_ROOT=/data/watch/library with ./data/watch mounted at /data/watch."""
    mount = tmp_path / "watch"
    (mount / "library").mkdir(parents=True)
    assert watch_root_problem(str(mount / "library"), _mountinfo(tmp_path, "/", str(mount))) is None


def test_watch_root_problem_root_not_on_a_mount(tmp_path):
    """WATCH_ROOT=/app: exists in the image, but nothing is mounted there."""
    root = tmp_path / "app"
    root.mkdir()
    problem = watch_root_problem(str(root), _mountinfo(tmp_path, "/", "/models"))
    assert problem is not None and "not on a mounted volume" in problem


def test_watch_root_problem_decodes_escaped_mount_points(tmp_path):
    root = tmp_path / "my docs"
    root.mkdir()
    escaped = str(root).replace(" ", "\\040")
    assert watch_root_problem(str(root), _mountinfo(tmp_path, "/", escaped)) is None


def test_watch_root_problem_without_mountinfo_checks_existence_only(tmp_path):
    root = tmp_path / "watch"
    root.mkdir()
    assert watch_root_problem(str(root), str(tmp_path / "no-such-mountinfo")) is None


def _logged_error(caplog, watch_root: str, service: str, mountinfo: str) -> str:
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.watcher.discovery"):
        problem = log_watch_root_problem(watch_root, service, mountinfo)
    assert problem is not None
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].startswith(f"{service}: ")
    return errors[0]


def test_log_watcher_missing_root_says_nothing_is_discovered(tmp_path, caplog):
    message = _logged_error(caplog, str(tmp_path / "missing"), "watcher", _mountinfo(tmp_path, "/"))
    assert "does not exist" in message and "No watched folders will be discovered" in message


def test_log_watcher_unmounted_root_says_extraction_fails(tmp_path, caplog):
    """Discovery works on a root outside the mount; the workers just can't read it."""
    root = tmp_path / "app"
    root.mkdir()
    message = _logged_error(caplog, str(root), "watcher", _mountinfo(tmp_path, "/"))
    assert "not on a mounted volume" in message
    assert "are discovered, but their documents will fail to extract" in message
    assert "will not be discovered" not in message


def test_log_api_problems_are_worded_for_the_app(tmp_path, caplog):
    root = tmp_path / "app"
    root.mkdir()
    unmounted = _logged_error(caplog, str(root), "api", _mountinfo(tmp_path, "/"))
    assert "cannot be downloaded" in unmounted and "discovered" not in unmounted
    caplog.clear()
    missing = _logged_error(caplog, str(tmp_path / "missing"), "api", _mountinfo(tmp_path, "/"))
    assert "cannot be downloaded" in missing and "discovered" not in missing


def test_log_watch_root_problem_is_silent_when_fine(tmp_path, caplog):
    root = tmp_path / "watch"
    root.mkdir()
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.watcher.discovery"):
        assert log_watch_root_problem(str(root), "watcher", _mountinfo(tmp_path, "/", str(root))) is None
    assert not caplog.records


def test_two_entries_resolving_to_one_directory_in_one_scan_make_one_row(sync_session, tmp_path):
    """`a` and `b -> a` in the same scan: the second must see the first's insert (#870 review)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").symlink_to(tmp_path / "a")

    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    rows = sync_session.query(WatchedFolder).all()
    assert [r.path for r in rows] == [str(tmp_path / "a")]
