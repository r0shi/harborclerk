"""Corpus-shaping actions are admin-only (#852).

Watched-folder management, mail-account management, document delete and
document reprocess decide what is indexed and searchable for every user of the
installation, so a signed-in non-admin human gets 403 on each and an admin gets
through. Reads keep `require_human_user` / `require_read_access`; uploads stay
open to every signed-in human (out of scope for #852).

The restricted set is enumerated from the routers themselves (method plus
path), not from a list kept here, so a write route added to one of these
routers later is held to the same gate without anyone remembering this file.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import auth_header

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _restricted_routes() -> list[tuple[str, str]]:
    """Every non-read (method, path) on the corpus-shaping routers, as mounted under /api."""
    from fastapi.routing import APIRoute

    from harbor_clerk.api.routes import documents, mail, watch

    found: set[tuple[str, str]] = set()
    for router in (watch.router, mail.router, documents.router):
        for route in router.routes:
            assert isinstance(route, APIRoute), route
            for method in route.methods - READ_METHODS:
                found.add((method, "/api" + route.path))
    return sorted(found)


RESTRICTED = _restricted_routes()

# The routes #852 names. The enumeration must contain each of them; if a router
# refactor made it come back empty or partial, the parametrized tests below
# would pass vacuously over what is left.
NAMED_IN_ISSUE = {
    ("POST", "/api/watch/folders"),
    ("PATCH", "/api/watch/folders/{folder_id}"),
    ("DELETE", "/api/watch/folders/{folder_id}"),
    ("POST", "/api/watch/folders/{folder_id}/rescan"),
    ("POST", "/api/watch/ingest"),
    ("POST", "/api/watch/remove"),
    ("POST", "/api/watch/rename"),
    ("POST", "/api/mail/accounts"),
    ("DELETE", "/api/mail/accounts/{account_id}"),
    ("POST", "/api/mail/labels"),
    ("DELETE", "/api/mail/labels/{label_id}"),
    ("DELETE", "/api/docs/{doc_id}"),
    ("POST", "/api/docs/{doc_id}/reprocess"),
    ("POST", "/api/docs/reprocess-failed"),
}


def _url(path: str) -> str:
    # Any well-formed id: the gate runs before the handler looks anything up,
    # so a non-admin is refused whether or not the row exists.
    for param in ("{folder_id}", "{account_id}", "{label_id}", "{doc_id}"):
        path = path.replace(param, str(uuid.uuid4()))
    assert "{" not in path, f"unfilled path parameter in {path}"
    return path


def test_enumeration_covers_every_route_the_issue_names() -> None:
    missing = NAMED_IN_ISSUE - set(RESTRICTED)
    assert not missing, f"restricted-route enumeration lost {sorted(missing)}"


@pytest.mark.parametrize(("method", "path"), RESTRICTED, ids=[f"{m} {p}" for m, p in RESTRICTED])
def test_route_depends_on_require_admin(method: str, path: str) -> None:
    """The gate is in the route's dependency tree, the same derivation gen_docs uses."""
    from harbor_clerk.api.app import create_app
    from scripts.gen_docs.generators.rest import _access_of, _walk_routes

    template = path.removeprefix("/api")
    matches = [r for r in _walk_routes(create_app()) if r.path == template and method in r.methods]
    assert len(matches) == 1, f"{method} {path} is not mounted exactly once: {matches}"
    assert _access_of(matches[0]) == "admin"


@pytest.mark.parametrize(("method", "path"), RESTRICTED, ids=[f"{m} {p}" for m, p in RESTRICTED])
async def test_non_admin_human_gets_403(client, regular_user, user_token, method: str, path: str) -> None:
    resp = await client.request(method, _url(path), json={}, headers=auth_header(user_token))
    assert resp.status_code == 403, f"{method} {path}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == "Admin access required"


@pytest.mark.parametrize(("method", "path"), RESTRICTED, ids=[f"{m} {p}" for m, p in RESTRICTED])
async def test_admin_passes_the_gate(client, admin_user, admin_token, method: str, path: str) -> None:
    """An admin is let through: what comes back is the handler's answer, not a refusal.

    The ids are random and the body empty, so the handler answers 404 or 422
    here; the success paths themselves are covered per route in
    tests/test_api_watch.py, tests/api/test_mail_routes.py,
    tests/test_api_documents.py and tests/api/test_reprocess_failed.py, all
    as admin.
    """
    resp = await client.request(method, _url(path), json={}, headers=auth_header(admin_token))
    assert resp.status_code not in (401, 403), f"{method} {path}: {resp.status_code} {resp.text}"
    assert resp.status_code < 500, f"{method} {path}: {resp.status_code} {resp.text}"


async def test_admin_manages_a_watched_folder_end_to_end(client, admin_user, admin_token, monkeypatch, tmp_path):
    """The admin path really succeeds, not merely passes the gate."""
    import harbor_clerk.config as config

    monkeypatch.setenv("WATCH_ROOT", "")
    monkeypatch.setattr(config, "_settings", None)
    headers = auth_header(admin_token)

    created = await client.post("/api/watch/folders", json={"path": str(tmp_path)}, headers=headers)
    assert created.status_code == 201, created.text
    folder_id = created.json()["folder_id"]

    patched = await client.patch(f"/api/watch/folders/{folder_id}", json={"enabled": False}, headers=headers)
    assert patched.status_code == 200, patched.text
    rescanned = await client.post(f"/api/watch/folders/{folder_id}/rescan", headers=headers)
    assert rescanned.status_code == 200, rescanned.text
    deleted = await client.delete(f"/api/watch/folders/{folder_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text


async def test_non_admin_still_reads_watched_folders(client, regular_user, user_token, monkeypatch) -> None:
    """Reads keep their gate: the Folders page still loads for a non-admin."""
    import harbor_clerk.config as config

    monkeypatch.setenv("WATCH_ROOT", "")
    monkeypatch.setattr(config, "_settings", None)
    headers = auth_header(user_token)
    for path in ("/api/watch/folders", "/api/watch/system", "/api/watch/allowed-extensions"):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"
