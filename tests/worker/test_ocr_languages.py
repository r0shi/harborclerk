"""Tests for OCR language resolution: preferences -> Tesseract -l arg.

These exercise the pure logic (no Tesseract subprocess invocation). The
end-to-end OCR path is intentionally not unit-tested here — it's
exercised manually via the macOS bundle and would require a tesseract
binary + traineddata fixtures.
"""

import hashlib

import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.languages import LANGUAGES, ArtifactSpec, LanguageSpec, Tool
from harbor_clerk.models import User
from harbor_clerk.models.enums import UserRole
from harbor_clerk.worker.ocr_languages import (
    get_enabled_languages_from_preferences,
    get_ocr_languages_for_doc,
    iso_to_tesseract,
    resolve_ocr_languages,
    tesseract_lang_arg,
)


@pytest.fixture(autouse=True)
def _isolated_lang_packs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))


def _install_french_pack(monkeypatch, httpserver: HTTPServer):
    """Patch LANGUAGES['fr'] to a fixture spec and install the OCR pack."""
    from harbor_clerk.lang_packs.manager import download_artifact

    payload = b"FRENCH-TRAINEDDATA-FIXTURE"
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
    result = download_artifact("fr", Tool.OCR)
    assert result.status == "installed"


# --- iso_to_tesseract ---


def test_iso_to_tesseract_known_mappings():
    assert iso_to_tesseract("en") == "eng"
    assert iso_to_tesseract("fr") == "fra"
    assert iso_to_tesseract("de") == "deu"


def test_iso_to_tesseract_falls_back_to_iso_code():
    """Unknown languages return the ISO code unchanged — many languages
    use the same code in both schemes."""
    assert iso_to_tesseract("xx") == "xx"


# --- tesseract_lang_arg ---


def test_tesseract_lang_arg_single():
    assert tesseract_lang_arg(["en"]) == "eng"


def test_tesseract_lang_arg_multiple():
    assert tesseract_lang_arg(["en", "fr"]) == "eng+fra"


# --- resolve_ocr_languages ---


def test_resolve_returns_english_only_when_no_packs_installed():
    """No French pack on disk → French is dropped from the list."""
    assert resolve_ocr_languages(["en", "fr"]) == ["en"]


def test_resolve_includes_french_when_pack_installed(monkeypatch, httpserver):
    _install_french_pack(monkeypatch, httpserver)
    result = resolve_ocr_languages(["en", "fr"])
    assert result == ["en", "fr"]


def test_resolve_always_includes_english_first():
    """Even if the caller passes only ['fr'], English is included as
    the safe fallback."""
    result = resolve_ocr_languages(["fr"])
    assert result == ["en"]
    # And won't include French because the pack isn't installed
    # (test_resolve_includes_french_when_pack_installed covers the
    # other direction)


def test_resolve_skips_unknown_codes():
    """Codes not in LANGUAGES are dropped (e.g. a stale prefs entry)."""
    assert resolve_ocr_languages(["en", "xx", "yy"]) == ["en"]


def test_resolve_dedupes():
    assert resolve_ocr_languages(["en", "en"]) == ["en"]


# --- get_enabled_languages_from_preferences ---


@pytest.mark.asyncio
async def test_get_enabled_languages_returns_default_when_no_admin(db_session):
    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        assert get_enabled_languages_from_preferences(sync_session) == ["en"]
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_get_enabled_languages_reads_admin_preference(db_session, admin_user):
    """The OCR worker is single-tenant and reads any admin user's preference
    as the global setting."""
    admin_user.preferences = {"enabled_languages": ["en", "fr"]}
    await db_session.flush()
    await db_session.commit()

    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        result = get_enabled_languages_from_preferences(sync_session)
    finally:
        sync_session.close()

    assert result == ["en", "fr"]


@pytest.mark.asyncio
async def test_get_enabled_languages_filters_unknown_codes(db_session):
    """If the prefs blob contains stale codes (e.g. a removed language)
    we silently drop them rather than passing them through to Tesseract."""
    from harbor_clerk.auth import hash_password

    admin = User(
        email="admin2@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.admin,
        is_active=True,
        preferences={"enabled_languages": ["en", "fr", "xx", "yy"]},
    )
    db_session.add(admin)
    await db_session.flush()
    await db_session.commit()

    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        result = get_enabled_languages_from_preferences(sync_session)
    finally:
        sync_session.close()

    assert "xx" not in result
    assert "yy" not in result
    assert "en" in result
    assert "fr" in result


@pytest.mark.asyncio
async def test_get_enabled_languages_inserts_english_if_missing(db_session):
    """Defense in depth: if somehow the prefs blob ended up without
    English, we put it back at index 0. The API normaliser does this
    too, but the OCR side guards against any path that bypassed it."""
    from harbor_clerk.auth import hash_password

    admin = User(
        email="admin3@test.com",
        password_hash=hash_password("TestPassword123"),
        role=UserRole.admin,
        is_active=True,
        preferences={"enabled_languages": ["fr"]},  # no en — shouldn't happen via API
    )
    db_session.add(admin)
    await db_session.flush()
    await db_session.commit()

    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        result = get_enabled_languages_from_preferences(sync_session)
    finally:
        sync_session.close()

    assert result[0] == "en"
    assert "fr" in result


# --- get_ocr_languages_for_doc (the integration helper) ---


@pytest.mark.asyncio
async def test_get_ocr_languages_for_doc_default_install(db_session):
    """Fresh install: no admin, no packs. Returns English only."""
    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        iso, lang_arg = get_ocr_languages_for_doc(sync_session)
    finally:
        sync_session.close()

    assert iso == ["en"]
    assert lang_arg == "eng"


@pytest.mark.asyncio
async def test_get_ocr_languages_for_doc_with_french_enabled_and_installed(
    db_session, admin_user, monkeypatch, httpserver
):
    """The headline use case: admin enabled French and the pack is
    installed. Worker should pass `-l eng+fra` to Tesseract."""
    admin_user.preferences = {"enabled_languages": ["en", "fr"]}
    await db_session.flush()
    await db_session.commit()
    _install_french_pack(monkeypatch, httpserver)

    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        iso, lang_arg = get_ocr_languages_for_doc(sync_session)
    finally:
        sync_session.close()

    assert iso == ["en", "fr"]
    assert lang_arg == "eng+fra"


@pytest.mark.asyncio
async def test_get_ocr_languages_for_doc_french_enabled_but_not_installed(db_session, admin_user):
    """Operator enabled French but never installed the pack. Tesseract
    can't do anything with it, so we silently drop French and OCR with
    English only — better than passing -l eng+fra and getting a
    'Failed loading language fra' warning every job."""
    admin_user.preferences = {"enabled_languages": ["en", "fr"]}
    await db_session.flush()
    await db_session.commit()

    from harbor_clerk.db_sync import get_sync_session

    sync_session = get_sync_session()
    try:
        iso, lang_arg = get_ocr_languages_for_doc(sync_session)
    finally:
        sync_session.close()

    assert iso == ["en"]
    assert lang_arg == "eng"
