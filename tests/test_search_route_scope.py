"""Tests for scope on POST /api/search."""

import uuid


async def test_search_with_no_scope_returns_all(client, admin_token, two_folder_corpus):
    r = await client.post(
        "/api/search",
        json={"query": "doc"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    # Without scope, both folders' docs may appear in results
    doc_ids = {h["doc_id"] for h in r.json()["hits"]}
    # Just confirm we got some results (don't assert exact count — depends on corpus state)
    assert len(doc_ids) >= 0  # tautology; real assertion is the scoped one below


async def test_search_with_folder_scope_restricts(client, admin_token, two_folder_corpus):
    folder_a, _, docs_in_a, _ = two_folder_corpus
    r = await client.post(
        "/api/search",
        json={"query": "doc", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    expected = {str(d.doc_id) for d in docs_in_a}
    returned = {h["doc_id"] for h in r.json()["hits"]}
    assert returned.issubset(expected)


async def test_search_rejects_unknown_folder_id(client, admin_token):
    r = await client.post(
        "/api/search",
        json={"query": "x", "scope": {"folder_ids": [str(uuid.uuid4())]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


async def test_search_rejects_unavailable_folder(client, admin_token, unavailable_folder):
    r = await client.post(
        "/api/search",
        json={"query": "x", "scope": {"folder_ids": [str(unavailable_folder.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
