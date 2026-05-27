"""Tests for scope field on chat conversation routes."""

import uuid


async def test_create_conversation_with_no_scope_field_defaults_to_empty(client, admin_token):
    r = await client.post(
        "/api/chat/conversations",
        json={"title": "Unscoped"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {}


async def test_create_conversation_with_folder_scope_persists(client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    r = await client.post(
        "/api/chat/conversations",
        json={"title": "Scoped", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}


async def test_create_conversation_rejects_unknown_folder_id(client, admin_token):
    r = await client.post(
        "/api/chat/conversations",
        json={"title": "Bad", "scope": {"folder_ids": [str(uuid.uuid4())]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


async def test_create_conversation_rejects_unavailable_folder(client, admin_token, unavailable_folder):
    r = await client.post(
        "/api/chat/conversations",
        json={"title": "Bad", "scope": {"folder_ids": [str(unavailable_folder.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


async def test_get_conversation_returns_scope(client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    create = await client.post(
        "/api/chat/conversations",
        json={"title": "S", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    conv_id = create.json()["conversation_id"]

    r = await client.get(
        f"/api/chat/conversations/{conv_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}
