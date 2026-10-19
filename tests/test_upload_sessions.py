"""Tests for upload session endpoints and the legacy /api/uploads endpoint."""

import io
import os

import pytest

from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _ensure_storage_dir():
    """Ensure the test storage directory exists."""
    path = os.environ.get("STORAGE_PATH", "/tmp/harbor_clerk_test_storage")
    os.makedirs(os.path.join(path, "originals"), exist_ok=True)


# --- Session CRUD ---


async def test_create_session(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 5, "auto_confirm": True, "label": "Test batch"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_files"] == 5
    assert data["auto_confirm"] is True
    assert data["label"] == "Test batch"
    assert data["status"] == "active"
    assert data["uploaded"] == 0
    assert data["confirmed"] == 0


async def test_get_session(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 3},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    resp = await client.get(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid
    assert resp.json()["auto_confirm"] is False


async def test_get_session_not_found(client, admin_user, admin_token):
    resp = await client.get(
        "/api/uploads/sessions/00000000-0000-0000-0000-000000000000",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


async def test_session_access_denied(client, admin_user, admin_token, regular_user, user_token):
    # Admin creates a session
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Regular user cannot access it
    resp = await client.get(f"/api/uploads/sessions/{sid}", headers=auth_header(user_token))
    assert resp.status_code == 403


# --- Per-file upload (auto-confirm) ---


async def test_upload_file_auto_confirm(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1, "auto_confirm": True},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Upload a file
    file_content = b"Hello, this is test content for auto-confirm."
    files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        data={"source_path": "folder/test.txt"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processing"
    assert data["filename"] == "test.txt"
    assert data["source_path"] == "folder/test.txt"
    assert data["doc_id"] is not None
    assert len(data["sha256"]) == 64  # hex sha256

    # Session should reflect upload
    resp = await client.get(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    session = resp.json()
    assert session["uploaded"] == 1
    assert session["confirmed"] == 1


# --- Per-file upload (review mode) ---


async def test_upload_file_review_mode(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1, "auto_confirm": False},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    file_content = b"Review mode test content."
    files = {"file": ("review.pdf", io.BytesIO(file_content), "application/pdf")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        data={"source_path": "review.pdf"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_confirmation"
    assert data["doc_id"] is None


# --- Duplicate detection ---


async def test_upload_file_duplicate_detection(client, admin_user, admin_token):
    # First upload: auto-confirm to create a version
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 2, "auto_confirm": True},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    content = b"Duplicate detection test content."
    files = {"file": ("orig.txt", io.BytesIO(content), "text/plain")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    assert resp.json()["status"] == "processing"

    # Second upload with same content
    files = {"file": ("copy.txt", io.BytesIO(content), "text/plain")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    data = resp.json()
    assert data["status"] == "duplicate"
    assert data["duplicate_doc_id"] is not None


# --- Session confirm ---


async def test_confirm_session(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 2, "auto_confirm": False},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Upload two files
    for name in ["a.txt", "b.txt"]:
        content = f"Content of {name}".encode()
        files = {"file": (name, io.BytesIO(content), "text/plain")}
        resp = await client.post(
            f"/api/uploads/sessions/{sid}/files",
            files=files,
            data={"source_path": name},
            headers=auth_header(admin_token),
        )
        assert resp.json()["status"] == "pending_confirmation"

    # Confirm all
    resp = await client.post(f"/api/uploads/sessions/{sid}/confirm", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 2
    assert all(r["status"] == "processing" for r in data["results"])
    assert all(r.get("doc_id") is not None for r in data["results"])

    # Session should be completed
    resp = await client.get(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    assert resp.json()["status"] == "completed"


async def test_confirm_auto_confirm_session_fails(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1, "auto_confirm": True},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    resp = await client.post(f"/api/uploads/sessions/{sid}/confirm", headers=auth_header(admin_token))
    assert resp.status_code == 400
    assert "auto-confirm" in resp.json()["detail"]


# --- Resume ---


async def test_resume_returns_completed_paths(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 3, "auto_confirm": True},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Upload two files
    for name in ["file1.txt", "file2.txt"]:
        content = f"Content of {name}".encode()
        files = {"file": (name, io.BytesIO(content), "text/plain")}
        await client.post(
            f"/api/uploads/sessions/{sid}/files",
            files=files,
            data={"source_path": f"docs/{name}"},
            headers=auth_header(admin_token),
        )

    # Resume endpoint should return the completed paths
    resp = await client.get(f"/api/uploads/sessions/{sid}/resume", headers=auth_header(admin_token))
    assert resp.status_code == 200
    paths = resp.json()["completed_paths"]
    assert set(paths) == {"docs/file1.txt", "docs/file2.txt"}


# --- Cancel ---


async def test_cancel_session(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1, "auto_confirm": False},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Upload a file
    files = {"file": ("cancel_me.txt", io.BytesIO(b"cancel test"), "text/plain")}
    await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )

    # Cancel
    resp = await client.delete(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Session should be cancelled
    resp = await client.get(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    assert resp.json()["status"] == "cancelled"


async def test_cancel_completed_session_fails(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1, "auto_confirm": False},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Upload and confirm
    files = {"file": ("done.txt", io.BytesIO(b"done"), "text/plain")}
    await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    await client.post(f"/api/uploads/sessions/{sid}/confirm", headers=auth_header(admin_token))

    # Cancel should fail
    resp = await client.delete(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))
    assert resp.status_code == 400


# --- Validation ---


async def test_upload_unsupported_extension(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    files = {"file": ("bad.exe", io.BytesIO(b"bad"), "application/octet-stream")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_upload_empty_file(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    files = {"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400
    assert "Empty" in resp.json()["detail"]


async def test_upload_to_inactive_session(client, admin_user, admin_token):
    resp = await client.post(
        "/api/uploads/sessions",
        json={"total_files": 1},
        headers=auth_header(admin_token),
    )
    sid = resp.json()["session_id"]

    # Cancel first
    await client.delete(f"/api/uploads/sessions/{sid}", headers=auth_header(admin_token))

    # Try to upload
    files = {"file": ("late.txt", io.BytesIO(b"too late"), "text/plain")}
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files=files,
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400
    assert "not active" in resp.json()["detail"]


# --- Legacy POST /api/uploads ---


async def test_legacy_upload_excalidraw_skipped(client, admin_user, admin_token):
    """POST /api/uploads must skip *.excalidraw.md files (status='skipped')."""
    file_content = b"excalidraw compressed blob"
    files = {"files": ("Diagram.excalidraw.md", io.BytesIO(file_content), "text/markdown")}
    resp = await client.post(
        "/api/uploads",
        files=files,
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    results = resp.json()["files"]
    assert len(results) == 1
    assert results[0]["filename"] == "Diagram.excalidraw.md"
    assert results[0]["status"] == "skipped"


# --- new_version on a watched document (#859) ---


async def _pending_upload(client, token, name: str, content: bytes) -> str:
    resp = await client.post(
        "/api/uploads/sessions", json={"total_files": 1, "auto_confirm": False}, headers=auth_header(token)
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/uploads/sessions/{sid}/files",
        files={"file": (name, io.BytesIO(content), "text/plain")},
        headers=auth_header(token),
    )
    assert resp.json()["status"] == "pending_confirmation"
    return resp.json()["upload_id"]


async def _document(db_session, *, watched: bool, tmp_path, removed: bool = False):
    import hashlib

    from harbor_clerk.models import Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

    source = tmp_path / "watched.txt"
    source.write_text("on disk")
    doc = Document(
        title="watched",
        canonical_filename="watched.txt",
        status="active",
        sha256=hashlib.sha256(b"on disk" + str(watched).encode()).digest(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        source_path=str(source) if watched else None,
    )
    db_session.add(doc)
    await db_session.flush()
    if watched:
        folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
        db_session.add(folder)
        await db_session.flush()
        db_session.add(
            WatchedFile(
                folder_id=folder.folder_id,
                relative_path=source.name,
                bookmark_data=b"",
                sha256=doc.sha256,
                doc_id=doc.doc_id,
                status=WatchedFileStatus.removed if removed else WatchedFileStatus.active,
            )
        )
    await db_session.commit()
    return doc


async def test_confirm_refuses_new_version_of_a_watched_document(client, db_session, admin_user, admin_token, tmp_path):
    """A watched document is read in place: new_version is a 409 and the document is unchanged."""
    from sqlalchemy import select

    from harbor_clerk.models import Document

    doc = await _document(db_session, watched=True, tmp_path=tmp_path)
    doc_id, source_path = doc.doc_id, doc.source_path
    upload_id = await _pending_upload(client, admin_token, "replacement.txt", b"replacement content 859")

    resp = await client.post(
        "/api/uploads/confirm",
        json={
            "upload_id": upload_id,
            "action": "new_version",
            "existing_doc_id": str(doc_id),
            "source_path": "/elsewhere/label.txt",
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert "watched folder" in resp.json()["detail"]

    db_session.expire_all()
    after = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert after.source_path == source_path
    assert after.original_object_key is None


async def test_confirm_batch_refuses_new_version_of_a_watched_document(
    client, db_session, admin_user, admin_token, tmp_path
):
    doc = await _document(db_session, watched=True, tmp_path=tmp_path)
    doc_id = doc.doc_id
    upload_id = await _pending_upload(client, admin_token, "replacement.txt", b"replacement content 859 batch")

    resp = await client.post(
        "/api/uploads/confirm-batch",
        json={"items": [{"upload_id": upload_id, "action": "new_version", "existing_doc_id": str(doc_id)}]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    (item,) = resp.json()["results"]
    assert item["doc_id"] is None
    assert "watched folder" in item["error"]


async def test_confirm_new_version_of_an_unwatched_document_still_succeeds(
    client, db_session, admin_user, admin_token, tmp_path
):
    doc = await _document(db_session, watched=False, tmp_path=tmp_path)
    doc_id = doc.doc_id
    upload_id = await _pending_upload(client, admin_token, "replacement.txt", b"replacement content 859 plain")

    resp = await client.post(
        "/api/uploads/confirm",
        json={"upload_id": upload_id, "action": "new_version", "existing_doc_id": str(doc_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["doc_id"] == str(doc_id)


async def test_confirm_refuses_new_version_of_a_document_whose_watched_file_is_removed(
    client, db_session, admin_user, admin_token, tmp_path
):
    """A document with a removed watched file is still a watched document: new_version is a 409."""
    doc = await _document(db_session, watched=True, tmp_path=tmp_path, removed=True)
    doc_id = doc.doc_id
    upload_id = await _pending_upload(client, admin_token, "replacement.txt", b"replacement content removed")

    resp = await client.post(
        "/api/uploads/confirm",
        json={"upload_id": upload_id, "action": "new_version", "existing_doc_id": str(doc_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
