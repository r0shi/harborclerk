import hashlib

import pytest

from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import JobStage, JobStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.events import EventKind, FileEvent, _should_ignore, handle_event


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


def test_modified_with_new_sha_reuses_existing_document(sync_session, folder, tmp_path):
    """Modify must add a new DocumentVersion to the existing Document, not create a new Document."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    wf = sync_session.query(WatchedFile).one()
    original_doc_id = wf.doc_id

    f.write_bytes(b"v2")
    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.doc_id == original_doc_id  # same Document
    # New version on the same Document
    doc = sync_session.query(Document).filter_by(doc_id=original_doc_id).one()
    assert doc.latest_version_id == wf.version_id


def test_dedup_by_sha_across_folders(sync_session, tmp_path):
    """A new file in folder B with the same sha as an existing file in folder A links to the existing Document."""
    folder_a = WatchedFolder(path="/tmp/a", display_name="a", bookmark_data=None)
    folder_b = WatchedFolder(path="/tmp/b", display_name="b", bookmark_data=None)
    sync_session.add_all([folder_a, folder_b])
    sync_session.commit()

    f_a = tmp_path / "a.pdf"
    f_a.write_bytes(b"shared content")
    handle_event(sync_session, FileEvent(EventKind.created, folder_a.folder_id, "a.pdf", str(f_a)))
    sync_session.commit()
    wf_a = sync_session.query(WatchedFile).filter_by(folder_id=folder_a.folder_id).one()

    f_b = tmp_path / "b.pdf"
    f_b.write_bytes(b"shared content")  # same content → same sha
    handle_event(sync_session, FileEvent(EventKind.created, folder_b.folder_id, "b.pdf", str(f_b)))
    sync_session.commit()

    wf_b = sync_session.query(WatchedFile).filter_by(folder_id=folder_b.folder_id).one()
    assert wf_b.doc_id == wf_a.doc_id
    # Only one Document exists
    assert sync_session.query(Document).count() == 1


def test_resurrect_with_same_sha_does_not_create_new_version(sync_session, folder, tmp_path):
    """If a removed file is recreated with the same sha, just flip active. No new Document/Version."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"hello")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    wf = sync_session.query(WatchedFile).one()
    original_version_id = wf.version_id

    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    f.write_bytes(b"hello")  # same content
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.active
    assert wf.removed_at is None
    assert wf.version_id == original_version_id  # NO new version
    assert sync_session.query(DocumentVersion).count() == 1


def test_resurrect_with_new_sha_creates_new_version_on_same_doc(sync_session, folder, tmp_path):
    """If a removed file is recreated with different content, treat like modify on existing doc."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    wf = sync_session.query(WatchedFile).one()
    original_doc_id = wf.doc_id
    original_version_id = wf.version_id

    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    f.write_bytes(b"v2")  # different content
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.active
    assert wf.removed_at is None
    assert wf.doc_id == original_doc_id  # same Document
    assert wf.version_id != original_version_id  # new Version


# --- _should_ignore filter tests ---


class TestShouldIgnore:
    def test_apple_double_top_level_ignored(self):
        assert _should_ignore("._Document.pdf") is True

    def test_apple_double_nested_ignored(self):
        assert _should_ignore("subfolder/._Document.pdf") is True

    def test_ds_store_ignored(self):
        assert _should_ignore(".DS_Store") is True
        assert _should_ignore("subdir/.DS_Store") is True

    def test_macosx_metadata_dir_ignored(self):
        assert _should_ignore("__MACOSX/foo.pdf") is True

    def test_dotfile_ignored(self):
        assert _should_ignore(".gitignore") is True
        assert _should_ignore(".git/HEAD") is True

    def test_unsupported_extension_ignored(self):
        assert _should_ignore("malware.exe") is True
        assert _should_ignore("library.dll") is True
        assert _should_ignore("vimswap.swp") is True

    def test_no_extension_ignored(self):
        # Files without an extension can't be classified — skip.
        assert _should_ignore("README") is True

    def test_allowed_files_pass(self):
        assert _should_ignore("contract.pdf") is False
        assert _should_ignore("notes.md") is False
        assert _should_ignore("subdir/photo.jpg") is False
        assert _should_ignore("Spreadsheet.XLSX") is False  # case insensitive


def test_handle_event_ignores_apple_double(sync_session, folder, tmp_path):
    """._foo.pdf events MUST NOT create a WatchedFile or IngestionJob."""
    f = tmp_path / "._stuff.pdf"
    f.write_bytes(b"binary metadata garbage")
    handle_event(
        sync_session,
        FileEvent(EventKind.created, folder.folder_id, "._stuff.pdf", str(f)),
    )
    sync_session.commit()
    assert sync_session.query(WatchedFile).count() == 0
    assert sync_session.query(IngestionJob).count() == 0


def test_handle_event_ignores_unsupported_extension(sync_session, folder, tmp_path):
    f = tmp_path / "malware.exe"
    f.write_bytes(b"MZ...")
    handle_event(
        sync_session,
        FileEvent(EventKind.created, folder.folder_id, "malware.exe", str(f)),
    )
    sync_session.commit()
    assert sync_session.query(WatchedFile).count() == 0


def test_handle_event_ignores_macosx_archive_metadata(sync_session, folder, tmp_path):
    sub = tmp_path / "__MACOSX"
    sub.mkdir()
    f = sub / "foo.pdf"
    f.write_bytes(b"metadata")
    handle_event(
        sync_session,
        FileEvent(EventKind.created, folder.folder_id, "__MACOSX/foo.pdf", str(f)),
    )
    sync_session.commit()
    assert sync_session.query(WatchedFile).count() == 0
