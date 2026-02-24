"""Tests for /api/docs endpoints."""

import uuid

import pytest

from harbor_clerk.models import Document
from tests.conftest import auth_header


async def test_list_documents_empty(client, admin_user, admin_token):
    resp = await client.get("/api/docs", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_documents_with_doc(client, admin_user, admin_token, db_session):
    doc = Document(title="Test Doc", status="active")
    db_session.add(doc)
    await db_session.flush()

    resp = await client.get("/api/docs", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Test Doc"


async def test_list_documents_excludes_deleted(client, admin_user, admin_token, db_session):
    doc_active = Document(title="Active", status="active")
    doc_deleted = Document(title="Deleted", status="deleted")
    db_session.add_all([doc_active, doc_deleted])
    await db_session.flush()

    resp = await client.get("/api/docs", headers=auth_header(admin_token))
    assert resp.status_code == 200
    titles = [d["title"] for d in resp.json()]
    assert "Active" in titles
    assert "Deleted" not in titles


async def test_list_documents_requires_auth(client):
    resp = await client.get("/api/docs")
    assert resp.status_code == 401


async def test_get_document_not_found(client, admin_user, admin_token):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/docs/{fake_id}", headers=auth_header(admin_token))
    assert resp.status_code == 404


async def test_delete_document_requires_admin(client, regular_user, user_token, db_session):
    doc = Document(title="To Delete", status="active")
    db_session.add(doc)
    await db_session.flush()

    resp = await client.delete(
        f"/api/docs/{doc.doc_id}",
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403
