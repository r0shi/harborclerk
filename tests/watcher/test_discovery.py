from harbor_clerk.models.watched import WatchedFolder
from harbor_clerk.watcher.discovery import scan_watch_root


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
