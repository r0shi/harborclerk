"""Tests for /api/docs endpoints."""

import uuid

from harbor_clerk.models import Chunk, Document, DocumentVersion, Entity
from harbor_clerk.models.enums import VersionStatus
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


async def test_corpus_overview_happy(client, admin_user, admin_token, db_session):
    doc = Document(title="Overview Doc", status="active")
    db_session.add(doc)
    await db_session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=b"sha256_for_overview_test_12345678",
        original_bucket="originals",
        original_object_key=f"originals/versions/{uuid.uuid4()}/test.pdf",
        mime_type="application/pdf",
        size_bytes=5000,
        status=VersionStatus.ready,
        source_path="test.pdf",
        summary="A summary.",
    )
    db_session.add(version)
    await db_session.flush()
    doc.latest_version_id = version.version_id

    for i in range(3):
        db_session.add(
            Chunk(
                version_id=version.version_id,
                doc_id=doc.doc_id,
                chunk_num=i,
                page_start=1,
                page_end=1,
                char_start=i * 100,
                char_end=(i + 1) * 100,
                chunk_text=f"Chunk {i}",
                language="en",
            )
        )
    await db_session.flush()

    resp = await client.get("/api/docs/overview", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_count"] == 1
    assert data["total_chunks"] == 3
    assert data["total_pages"] == 0
    assert data["languages"] == {"en": 3}
    assert data["mime_types"] == {"application/pdf": 1}
    assert data["date_range"]["oldest"] is not None
    assert data["date_range"]["newest"] is not None
    assert len(data["documents"]) == 1
    assert data["documents"][0]["title"] == "Overview Doc"
    assert data["truncated"] is False


async def test_corpus_overview_empty(client, admin_user, admin_token):
    resp = await client.get("/api/docs/overview", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_count"] == 0
    assert data["total_chunks"] == 0
    assert data["total_pages"] == 0
    assert data["languages"] == {}
    assert data["mime_types"] == {}
    assert data["date_range"] == {"oldest": None, "newest": None}
    assert data["documents"] == []


async def test_find_related_happy(client, admin_user, admin_token, db_session):
    doc1 = Document(title="ML Guide", status="active")
    doc2 = Document(title="DL Intro", status="active")
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    for doc in [doc1, doc2]:
        ver = DocumentVersion(
            doc_id=doc.doc_id,
            original_sha256=f"sha_{doc.title[:6]}".encode().ljust(31, b"_"),
            original_bucket="originals",
            original_object_key=f"originals/versions/{doc.doc_id}/f.pdf",
            mime_type="application/pdf",
            size_bytes=1000,
            status=VersionStatus.ready,
            source_path="f.pdf",
        )
        db_session.add(ver)
        await db_session.flush()
        doc.latest_version_id = ver.version_id
    await db_session.flush()

    emb = [0.9, 0.1] + [0.0] * 382
    for doc in [doc1, doc2]:
        db_session.add(
            Chunk(
                version_id=doc.latest_version_id,
                doc_id=doc.doc_id,
                chunk_num=0,
                page_start=1,
                page_end=1,
                char_start=0,
                char_end=100,
                chunk_text="text",
                language="en",
                embedding=emb,
            )
        )
    await db_session.flush()

    resp = await client.get(f"/api/docs/{doc1.doc_id}/related", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_id"] == str(doc1.doc_id)
    assert len(data["related"]) == 1
    assert data["related"][0]["title"] == "DL Intro"
    assert data["related"][0]["similarity"] > 0


async def test_find_related_not_found(client, admin_user, admin_token):
    resp = await client.get(f"/api/docs/{uuid.uuid4()}/related", headers=auth_header(admin_token))
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


async def test_get_document_entities_happy(client, admin_user, admin_token, db_session):
    doc = Document(title="Entity Doc", status="active")
    db_session.add(doc)
    await db_session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=b"sha256_for_entity_test_12345678",
        original_bucket="originals",
        original_object_key=f"originals/versions/{doc.doc_id}/test.pdf",
        mime_type="application/pdf",
        size_bytes=5000,
        status=VersionStatus.ready,
        source_path="test.pdf",
    )
    db_session.add(version)
    await db_session.flush()
    doc.latest_version_id = version.version_id

    chunk = Chunk(
        version_id=version.version_id,
        doc_id=doc.doc_id,
        chunk_num=0,
        page_start=1,
        page_end=1,
        char_start=0,
        char_end=100,
        chunk_text="John Smith works at Acme Corp.",
        language="en",
    )
    db_session.add(chunk)
    await db_session.flush()

    db_session.add_all(
        [
            Entity(
                version_id=version.version_id,
                chunk_id=chunk.chunk_id,
                doc_id=doc.doc_id,
                entity_text="John Smith",
                entity_type="PERSON",
                start_char=0,
                end_char=10,
            ),
            Entity(
                version_id=version.version_id,
                chunk_id=chunk.chunk_id,
                doc_id=doc.doc_id,
                entity_text="Acme Corp",
                entity_type="ORG",
                start_char=20,
                end_char=29,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.get(f"/api/docs/{doc.doc_id}/entities", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_id"] == str(doc.doc_id)
    assert data["total"] == 2
    assert len(data["entities"]) == 2
    assert set(data["entity_types"]) == {"PERSON", "ORG"}
    names = {e["entity_text"] for e in data["entities"]}
    assert "John Smith" in names
    assert "Acme Corp" in names


async def test_get_document_entities_empty(client, admin_user, admin_token, db_session):
    doc = Document(title="Empty Entity Doc", status="active")
    db_session.add(doc)
    await db_session.flush()

    version = DocumentVersion(
        doc_id=doc.doc_id,
        original_sha256=b"sha256_for_empty_entity_test_12",
        original_bucket="originals",
        original_object_key=f"originals/versions/{doc.doc_id}/test.pdf",
        mime_type="application/pdf",
        size_bytes=1000,
        status=VersionStatus.ready,
        source_path="test.pdf",
    )
    db_session.add(version)
    await db_session.flush()
    doc.latest_version_id = version.version_id
    await db_session.flush()

    resp = await client.get(f"/api/docs/{doc.doc_id}/entities", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["entities"] == []
    assert data["entity_types"] == []
