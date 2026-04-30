import hashlib

import pytest

from harbor_clerk.models.enums import JobStage, JobStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.events import EventKind, FileEvent, handle_event


@pytest.fixture
def folder(sync_session):
    f = WatchedFolder(path="/tmp/test", display_name="test", bookmark_data=None)
    sync_session.add(f)
    sync_session.commit()
    return f


def test_created_file_creates_watched_file_and_extract_job(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"hello world")
    event = FileEvent(
        kind=EventKind.created,
        folder_id=folder.folder_id,
        relative_path="doc.pdf",
        absolute_path=str(f),
    )
    handle_event(sync_session, event)
    sync_session.commit()

    wf = sync_session.query(WatchedFile).filter_by(folder_id=folder.folder_id).one()
    assert wf.relative_path == "doc.pdf"
    assert wf.sha256 == hashlib.sha256(b"hello world").digest()
    assert wf.status == WatchedFileStatus.active

    job = sync_session.query(IngestionJob).filter_by(version_id=wf.version_id, stage=JobStage.extract).one()
    assert job.status == JobStatus.queued


def test_modified_file_with_new_sha_creates_new_version(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    v1 = sync_session.query(WatchedFile).one().version_id

    f.write_bytes(b"v2")
    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.version_id != v1
    assert wf.sha256 == hashlib.sha256(b"v2").digest()


def test_deleted_file_marks_watched_file_removed(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.removed
    assert wf.removed_at is not None


def test_modified_with_same_sha_is_noop(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"unchanged")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    v1 = sync_session.query(WatchedFile).one().version_id

    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.version_id == v1
