"""Tests for upload session endpoints."""

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
    assert data["version_id"] is not None
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
    assert data["version_id"] is None


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
