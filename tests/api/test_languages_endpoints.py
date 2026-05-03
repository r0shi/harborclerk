"""Tests for /api/languages endpoints.

Mocks the actual download by patching the LANGUAGES static map to point
at httpserver fixtures, so install/remove paths exercise real code
without hitting upstream servers.
"""

import hashlib

import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.lang_packs import manager as lp_manager
from harbor_clerk.languages import LANGUAGES, ArtifactSpec, LanguageSpec, Tool
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _isolated_lang_packs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))


@pytest.fixture
def patched_french(monkeypatch, httpserver: HTTPServer):
    """Replace LANGUAGES['fr'] with a spec whose URLs point at httpserver."""
    payload = b"FAKE-TRAINEDDATA-CONTENT-FOR-API-TESTS"
    sha = hashlib.sha256(payload).hexdigest()
    httpserver.expect_request("/fra.traineddata").respond_with_data(payload)
    fake = LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url=httpserver.url_for("/fra.traineddata"),
                sha256=sha,
                size_bytes=len(payload),
                install_subpath="tesseract/fra.traineddata",
            ),
        },
    )
    monkeypatch.setitem(LANGUAGES, "fr", fake)
    return fake


# --- list_languages ---


async def test_list_languages_returns_curated_set(client, admin_user, admin_token):
    resp = await client.get("/api/languages", headers=auth_header(admin_token))
    assert resp.status_code == 200
    codes = {lang["code"] for lang in resp.json()["languages"]}
    assert "en" in codes
    assert "fr" in codes


async def test_list_languages_marks_english_as_built_in(client, admin_user, admin_token):
    resp = await client.get("/api/languages", headers=auth_header(admin_token))
    en = next(lang for lang in resp.json()["languages"] if lang["code"] == "en")
    assert en["built_in"] is True
    assert en["tools"] == {}
    # English is always considered "enabled" — it's bundled
    assert en["enabled"] is True


async def test_list_languages_marks_french_not_installed_initially(client, admin_user, admin_token):
    resp = await client.get("/api/languages", headers=auth_header(admin_token))
    fr = next(lang for lang in resp.json()["languages"] if lang["code"] == "fr")
    assert fr["built_in"] is False
    for tool_status in fr["tools"].values():
        assert tool_status["status"] == "not_installed"


async def test_list_languages_reflects_user_enabled_languages_preference(client, admin_user, admin_token, db_session):
    """When the operator's preference includes 'fr', the listing reports
    French as enabled. Mirrors what the UI uses to render the toggle state."""
    admin_user.preferences = {"enabled_languages": ["en", "fr"]}
    await db_session.flush()
    await db_session.commit()

    resp = await client.get("/api/languages", headers=auth_header(admin_token))
    assert resp.status_code == 200
    fr = next(lang for lang in resp.json()["languages"] if lang["code"] == "fr")
    assert fr["enabled"] is True


async def test_list_languages_defaults_to_english_only_enabled(client, admin_user, admin_token):
    """Fresh install: no enabled_languages preference → English only."""
    resp = await client.get("/api/languages", headers=auth_header(admin_token))
    assert resp.status_code == 200
    payload = resp.json()
    en = next(lang for lang in payload["languages"] if lang["code"] == "en")
    fr = next(lang for lang in payload["languages"] if lang["code"] == "fr")
    assert en["enabled"] is True
    assert fr["enabled"] is False


async def test_list_languages_requires_auth(client):
    resp = await client.get("/api/languages")
    assert resp.status_code == 401


# --- install ---


async def test_install_french_ocr_succeeds_with_admin(client, admin_user, admin_token, patched_french):
    resp = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["tool"] == "ocr"
    assert body["results"][0]["status"] == "installed"
    assert body["results"][0]["error"] is None
    # After install, list should reflect installed state
    list_resp = await client.get("/api/languages", headers=auth_header(admin_token))
    fr = next(lang for lang in list_resp.json()["languages"] if lang["code"] == "fr")
    assert fr["tools"]["ocr"]["status"] == "installed"


async def test_install_is_idempotent(client, admin_user, admin_token, patched_french):
    first = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert first.json()["results"][0]["status"] == "installed"

    second = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert second.json()["results"][0]["status"] == "already_installed"


async def test_install_requires_admin(client, regular_user, user_token, patched_french):
    resp = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403


async def test_install_unknown_language_returns_404(client, admin_user, admin_token):
    resp = await client.post(
        "/api/languages/xx/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


async def test_install_unknown_tool_returns_422(client, admin_user, admin_token):
    resp = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["bogus"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_install_tool_with_no_artifact_returns_422(client, admin_user, admin_token):
    """English has no artifacts — asking to install English/OCR is invalid."""
    resp = await client.post(
        "/api/languages/en/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_install_failure_is_reported_per_tool_not_thrown(client, admin_user, admin_token, monkeypatch):
    """If download_artifact fails (server down, SHA mismatch, etc.), the
    response is 200 with a per-tool failure entry — callers shouldn't have
    to thread try/except around install requests."""

    def _fail(*args, **kwargs):
        return lp_manager.DownloadResult(status="failed", error="simulated network error")

    monkeypatch.setattr(lp_manager, "download_artifact", _fail)
    # languages.py imports the function by name into its module namespace
    from harbor_clerk.api.routes import languages as lang_routes

    monkeypatch.setattr(lang_routes, "download_artifact", _fail)

    resp = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["status"] == "failed"
    assert "simulated network error" in result["error"]


# --- remove ---


async def test_remove_single_tool(client, admin_user, admin_token, patched_french):
    # Install first so there's something to remove
    await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    resp = await client.delete(
        "/api/languages/fr/install/ocr",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"
    list_resp = await client.get("/api/languages", headers=auth_header(admin_token))
    fr = next(lang for lang in list_resp.json()["languages"] if lang["code"] == "fr")
    assert fr["tools"]["ocr"]["status"] == "not_installed"


async def test_remove_is_idempotent(client, admin_user, admin_token):
    """Removing a tool that isn't installed is fine."""
    resp = await client.delete(
        "/api/languages/fr/install/ocr",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200


async def test_remove_language_completely(client, admin_user, admin_token, patched_french):
    await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers=auth_header(admin_token),
    )
    resp = await client.delete("/api/languages/fr", headers=auth_header(admin_token))
    assert resp.status_code == 200


async def test_remove_requires_admin(client, regular_user, user_token):
    resp = await client.delete(
        "/api/languages/fr/install/ocr",
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403
