import hashlib

import pytest

from harbor_clerk.models.chunk import Chunk
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_heading import DocumentHeading
from harbor_clerk.models.document_page import DocumentPage
from harbor_clerk.models.entity import Entity
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.events import EventKind, FileEvent, _should_ignore, handle_event


@pytest.fixture
def folder(sync_session):
    f = WatchedFolder(path="/tmp/test", display_name="test", bookmark_data=None)
    sync_session.add(f)
    sync_session.commit()
    return f


# ---------------------------------------------------------------------------
# Core state-machine tests (4 active-event branches + deleted-event branch)
# ---------------------------------------------------------------------------


def test_new_file_creates_document_and_extract_job(sync_session, folder, tmp_path):
    """A created event for a fresh path → 1 Document, 1 IngestionJob (extract)."""
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

    assert sync_session.query(Document).count() == 1
    doc = sync_session.query(Document).one()
    assert doc.pipeline_status == PipelineStatus.queued
    assert doc.pipeline_seq == 0
    assert doc.sha256 == hashlib.sha256(b"hello world").digest()

    wf = sync_session.query(WatchedFile).one()
    assert wf.relative_path == "doc.pdf"
    assert wf.sha256 == hashlib.sha256(b"hello world").digest()
    assert wf.status == WatchedFileStatus.active
    assert wf.doc_id == doc.doc_id

    job = sync_session.query(IngestionJob).one()
    assert job.doc_id == doc.doc_id
    assert job.stage == JobStage.extract
    assert job.status == JobStatus.queued


def test_modify_same_sha_is_noop(sync_session, folder, tmp_path):
    """Branch 1: existing+active+same SHA → no-op (no new job, no state change)."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"unchanged")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    original_doc = sync_session.query(Document).one()
    original_seq = original_doc.pipeline_seq

    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    assert sync_session.query(Document).count() == 1
    doc = sync_session.query(Document).one()
    assert doc.pipeline_seq == original_seq
    # Only one job from the original create
    assert sync_session.query(IngestionJob).count() == 1


def test_modify_replaces_in_place(sync_session, folder, tmp_path):
    """Branch 2: existing+active+different SHA → bumps pipeline_seq, deletes children, enqueues extract.

    The doc_id is preserved (chat citations / conversation references stay valid).
    Old chunks/entities/pages/headings/jobs are wiped.
    """
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    original_doc_id = wf.doc_id

    # Simulate child rows that exist after v1 ingestion
    doc = sync_session.query(Document).one()
    chunk = Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="old content")
    page = DocumentPage(doc_id=doc.doc_id, page_num=1)
    heading = DocumentHeading(doc_id=doc.doc_id, level=1, title="Old heading", position=0)
    sync_session.add_all([chunk, page, heading])
    sync_session.flush()
    entity = Entity(
        chunk_id=chunk.chunk_id, doc_id=doc.doc_id, entity_text="Alice", entity_type="PERSON", start_char=0, end_char=5
    )
    sync_session.add(entity)
    sync_session.commit()

    # Sanity: child rows exist
    assert sync_session.query(Chunk).count() == 1
    assert sync_session.query(DocumentPage).count() == 1
    assert sync_session.query(DocumentHeading).count() == 1
    assert sync_session.query(Entity).count() == 1

    # Write new content and fire a modify event
    f.write_bytes(b"v2")
    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    doc = sync_session.query(Document).one()
    assert doc.doc_id == original_doc_id  # same doc, replace in place
    assert doc.pipeline_seq == 1  # was 0
    assert doc.sha256 == hashlib.sha256(b"v2").digest()
    assert doc.pipeline_status == PipelineStatus.queued
    assert doc.error is None

    # Old child rows wiped
    assert sync_session.query(Chunk).count() == 0
    assert sync_session.query(Entity).count() == 0
    assert sync_session.query(DocumentPage).count() == 0
    assert sync_session.query(DocumentHeading).count() == 0

    # Exactly one new extract job for the same doc_id
    jobs = sync_session.query(IngestionJob).all()
    assert len(jobs) == 1
    assert jobs[0].doc_id == original_doc_id
    assert jobs[0].stage == JobStage.extract


def test_no_cross_path_dedup(sync_session, tmp_path):
    """Two different paths with the same SHA → two distinct Documents (no cross-content dedup)."""
    folder_a = WatchedFolder(path="/tmp/a", display_name="a", bookmark_data=None)
    folder_b = WatchedFolder(path="/tmp/b", display_name="b", bookmark_data=None)
    sync_session.add_all([folder_a, folder_b])
    sync_session.commit()

    f_a = tmp_path / "a.pdf"
    f_a.write_bytes(b"shared content")
    handle_event(sync_session, FileEvent(EventKind.created, folder_a.folder_id, "a.pdf", str(f_a)))
    sync_session.commit()

    f_b = tmp_path / "b.pdf"
    f_b.write_bytes(b"shared content")  # same content → same SHA
    handle_event(sync_session, FileEvent(EventKind.created, folder_b.folder_id, "b.pdf", str(f_b)))
    sync_session.commit()

    # Two independent Documents (no dedup)
    assert sync_session.query(Document).count() == 2
    assert sync_session.query(WatchedFile).count() == 2
    assert sync_session.query(IngestionJob).count() == 2

    doc_ids = {wf.doc_id for wf in sync_session.query(WatchedFile).all()}
    assert len(doc_ids) == 2  # each WatchedFile points to a different Document


def test_deleted_event_marks_removed(sync_session, folder, tmp_path):
    """Active row + deleted event → status=removed, removed_at set."""
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


def test_resurrect_same_sha(sync_session, folder, tmp_path):
    """Branch 3: removed row + created event with same SHA → status=active, no new ingestion."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"hello")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    original_doc_id = sync_session.query(WatchedFile).one().doc_id
    original_seq = sync_session.query(Document).one().pipeline_seq

    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    f.write_bytes(b"hello")  # same content
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.active
    assert wf.removed_at is None
    assert wf.doc_id == original_doc_id

    doc = sync_session.query(Document).one()
    assert doc.pipeline_seq == original_seq  # no re-ingest
    # No new jobs beyond the original extract job (which may have been deleted/run)
    # But there must NOT be a second extract job created for the resurrect
    assert sync_session.query(IngestionJob).count() == 1  # only the original


def test_resurrect_different_sha(sync_session, folder, tmp_path):
    """Branch 4: removed row + created event with different SHA → status=active + reprocess."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    original_doc_id = sync_session.query(WatchedFile).one().doc_id

    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    f.write_bytes(b"v2")  # different content
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.active
    assert wf.removed_at is None
    assert wf.doc_id == original_doc_id  # same Document (replace in place)

    doc = sync_session.query(Document).one()
    assert doc.pipeline_seq == 1  # was 0
    assert doc.sha256 == hashlib.sha256(b"v2").digest()
    assert doc.pipeline_status == PipelineStatus.queued

    # One extract job (old deleted, new one created)
    jobs = sync_session.query(IngestionJob).all()
    assert len(jobs) == 1
    assert jobs[0].doc_id == original_doc_id
    assert jobs[0].stage == JobStage.extract


# ---------------------------------------------------------------------------
# 0-byte file regression tests (preserved from PR #252)
# ---------------------------------------------------------------------------


def test_zero_byte_file_is_skipped(sync_session, folder, tmp_path):
    """Regression: 0-byte files (network share mid-copy, etc.) MUST NOT be ingested."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"")  # 0 bytes
    handle_event(
        sync_session,
        FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)),
    )
    sync_session.commit()
    assert sync_session.query(WatchedFile).count() == 0
    assert sync_session.query(Document).count() == 0


def test_zero_byte_files_dont_collide_into_one_doc(sync_session, folder, tmp_path):
    """Regression for the empty-sha-collision bug: two 0-byte files at
    different paths must not get linked to a single Document via dedup."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"")
    b.write_bytes(b"")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "a.pdf", str(a)))
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "b.pdf", str(b)))
    sync_session.commit()
    # Neither should have been ingested at all (both 0 bytes).
    assert sync_session.query(WatchedFile).count() == 0
    assert sync_session.query(Document).count() == 0


def test_file_grows_from_zero_to_real_content_ingests_normally(sync_session, folder, tmp_path):
    """After the 0-byte event is skipped, the next event with real content
    must ingest cleanly — with no stale watched_file row poisoning the
    'active+different sha' modify branch."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    assert sync_session.query(WatchedFile).count() == 0  # skipped

    f.write_bytes(b"real content")
    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wfs = sync_session.query(WatchedFile).all()
    assert len(wfs) == 1
    assert wfs[0].sha256 == hashlib.sha256(b"real content").digest()

    doc = sync_session.query(Document).one()
    assert doc.pipeline_status == PipelineStatus.queued


# ---------------------------------------------------------------------------
# _should_ignore filter tests
# ---------------------------------------------------------------------------


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

    def test_newly_allowed_extensions_pass(self):
        """Phase 1 broadened the allowlist — these text formats now ingest."""
        for name in ("notes.rst", "script.py", "config.toml", "data.json", "subs.srt", "doc.markdown"):
            assert _should_ignore(name) is False, name

    def test_excalidraw_md_ignored(self):
        """*.excalidraw.md carries a JSON blob, not prose — must be skipped."""
        assert _should_ignore("Diagram.excalidraw.md") is True
        assert _should_ignore("vault/sub/Sketch.excalidraw.md") is True


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
