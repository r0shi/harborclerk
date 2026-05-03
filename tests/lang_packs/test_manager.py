"""Download manager tests against pytest-httpserver fixtures.

Real upstream URLs would make tests flaky and slow; we patch the
LANGUAGES map to point at a local fixture server instead. This exercises
the full download → verify → install flow against artifacts whose SHAs
we control at test time.
"""

import hashlib

import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.lang_packs.manager import (
    DownloadResult,
    download_artifact,
    installed_languages,
    installed_tools_for,
    remove_artifact,
    remove_language,
    verify_artifact,
)
from harbor_clerk.lang_packs.storage import artifact_path, lang_dir
from harbor_clerk.languages import LANGUAGES, ArtifactSpec, LanguageSpec, Tool


@pytest.fixture(autouse=True)
def _isolated_lang_packs_dir(monkeypatch, tmp_path):
    """Every test gets a fresh lang_packs_dir under tmp_path."""
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))


def _patch_french_to_local_server(monkeypatch, httpserver: HTTPServer, payload: bytes, with_ner: bool = False):
    """Replace the LANGUAGES['fr'] entry with one whose URLs point at
    httpserver and whose SHAs match the supplied payload(s).

    Returns the patched LanguageSpec for assertion access.
    """
    sha = hashlib.sha256(payload).hexdigest()
    httpserver.expect_request("/fra.traineddata").respond_with_data(payload)
    artifacts = {
        Tool.OCR: ArtifactSpec(
            url=httpserver.url_for("/fra.traineddata"),
            sha256=sha,
            size_bytes=len(payload),
            install_subpath="tesseract/fra.traineddata",
        ),
    }
    if with_ner:
        ner_payload = b"FAKE-WHEEL-CONTENT-FOR-NER-TESTS"
        ner_sha = hashlib.sha256(ner_payload).hexdigest()
        httpserver.expect_request("/fr_core_news_sm-3.8.0-py3-none-any.whl").respond_with_data(ner_payload)
        artifacts[Tool.NER] = ArtifactSpec(
            url=httpserver.url_for("/fr_core_news_sm-3.8.0-py3-none-any.whl"),
            sha256=ner_sha,
            size_bytes=len(ner_payload),
            install_subpath="ner/fr_core_news_sm-3.8.0-py3-none-any.whl",
        )
    fake = LanguageSpec(code="fr", display_name="French (test)", artifacts=artifacts)
    monkeypatch.setitem(LANGUAGES, "fr", fake)
    return fake


def test_download_writes_file_and_verifies(monkeypatch, httpserver, tmp_path):
    payload = b"FAKE-TRAINEDDATA-CONTENT-WITH-KNOWN-HASH"
    _patch_french_to_local_server(monkeypatch, httpserver, payload)

    result = download_artifact("fr", Tool.OCR)

    assert isinstance(result, DownloadResult)
    assert result.status == "installed"
    assert result.bytes_downloaded == len(payload)
    target = tmp_path / "fr" / "tesseract" / "fra.traineddata"
    assert target.read_bytes() == payload
    assert verify_artifact("fr", Tool.OCR) is True


def test_download_is_idempotent(monkeypatch, httpserver, tmp_path):
    """Second call against an already-installed artifact returns
    already_installed without re-fetching."""
    payload = b"IDEMPOTENT-CONTENT"
    _patch_french_to_local_server(monkeypatch, httpserver, payload)

    first = download_artifact("fr", Tool.OCR)
    assert first.status == "installed"

    second = download_artifact("fr", Tool.OCR)
    assert second.status == "already_installed"
    assert second.bytes_downloaded == 0


def test_download_fails_on_sha_mismatch(monkeypatch, httpserver, tmp_path):
    """Server returns content whose SHA doesn't match what the static
    map promises. The download must fail AND must not leave a
    half-installed artifact on disk for a future verify_artifact() to
    misinterpret."""
    httpserver.expect_request("/fra.traineddata").respond_with_data(b"WRONG-CONTENT")
    fake = LanguageSpec(
        code="fr",
        display_name="French (test)",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url=httpserver.url_for("/fra.traineddata"),
                sha256="0" * 64,  # never matches
                size_bytes=10,
                install_subpath="tesseract/fra.traineddata",
            ),
        },
    )
    monkeypatch.setitem(LANGUAGES, "fr", fake)

    result = download_artifact("fr", Tool.OCR)

    assert result.status == "failed"
    assert "sha256" in result.error.lower()
    target = tmp_path / "fr" / "tesseract" / "fra.traineddata"
    assert not target.exists(), "Failed downloads must not leave a partial file"
    # Sibling .tmp file should also be cleaned up
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_download_fails_on_http_error(monkeypatch, httpserver, tmp_path):
    """A 404 / 500 from upstream should produce a failed result, not
    raise — callers don't want to thread try/except around every call."""
    httpserver.expect_request("/fra.traineddata").respond_with_data(b"missing", status=404)
    fake = LanguageSpec(
        code="fr",
        display_name="French (test)",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url=httpserver.url_for("/fra.traineddata"),
                sha256="0" * 64,
                size_bytes=1,
                install_subpath="tesseract/fra.traineddata",
            ),
        },
    )
    monkeypatch.setitem(LANGUAGES, "fr", fake)

    result = download_artifact("fr", Tool.OCR)

    assert result.status == "failed"
    assert result.error
    target = tmp_path / "fr" / "tesseract" / "fra.traineddata"
    assert not target.exists()


def test_download_unknown_language_returns_failed_not_raises():
    result = download_artifact("xx", Tool.OCR)
    assert result.status == "failed"
    assert "unknown language" in result.error


def test_download_language_without_tool_artifact_returns_failed():
    """English has no artifacts — asking for English/OCR is an error."""
    result = download_artifact("en", Tool.OCR)
    assert result.status == "failed"
    assert "no ocr artifact" in result.error.lower()


def test_verify_artifact_false_when_missing(monkeypatch, httpserver, tmp_path):
    _patch_french_to_local_server(monkeypatch, httpserver, b"x")
    assert verify_artifact("fr", Tool.OCR) is False


def test_verify_artifact_false_when_corrupted(monkeypatch, httpserver, tmp_path):
    payload = b"ORIGINAL"
    _patch_french_to_local_server(monkeypatch, httpserver, payload)
    download_artifact("fr", Tool.OCR)

    # Tamper with the on-disk file
    target = tmp_path / "fr" / "tesseract" / "fra.traineddata"
    target.write_bytes(b"TAMPERED")

    assert verify_artifact("fr", Tool.OCR) is False


def test_remove_artifact_deletes_file(monkeypatch, httpserver, tmp_path):
    _patch_french_to_local_server(monkeypatch, httpserver, b"removable")
    download_artifact("fr", Tool.OCR)
    target = artifact_path("fr", Tool.OCR)
    assert target.exists()

    remove_artifact("fr", Tool.OCR)

    assert not target.exists()


def test_remove_artifact_cleans_empty_intermediate_dirs(monkeypatch, httpserver, tmp_path):
    """The empty ``tesseract/`` subdir should go away after the only
    artifact in it is removed. The per-language root (``fr/``) stays."""
    _patch_french_to_local_server(monkeypatch, httpserver, b"removable")
    download_artifact("fr", Tool.OCR)

    remove_artifact("fr", Tool.OCR)

    # The tesseract subdir should be cleaned up
    assert not (tmp_path / "fr" / "tesseract").exists()
    # But the lang root stays (may host other tools)
    assert lang_dir("fr").exists()


def test_remove_artifact_idempotent(monkeypatch, httpserver, tmp_path):
    """Removing twice doesn't raise."""
    _patch_french_to_local_server(monkeypatch, httpserver, b"x")
    remove_artifact("fr", Tool.OCR)  # not installed yet
    remove_artifact("fr", Tool.OCR)  # still not installed


def test_remove_language_clears_everything(monkeypatch, httpserver, tmp_path):
    """Disabling French should wipe the entire fr/ tree, not just one tool."""
    _patch_french_to_local_server(monkeypatch, httpserver, b"x", with_ner=True)
    download_artifact("fr", Tool.OCR)
    download_artifact("fr", Tool.NER)
    assert lang_dir("fr").exists()

    remove_language("fr")

    assert not lang_dir("fr").exists()


def test_installed_tools_for_returns_only_verified(monkeypatch, httpserver, tmp_path):
    _patch_french_to_local_server(monkeypatch, httpserver, b"x", with_ner=True)
    assert installed_tools_for("fr") == set()

    download_artifact("fr", Tool.OCR)
    assert installed_tools_for("fr") == {Tool.OCR}

    download_artifact("fr", Tool.NER)
    assert installed_tools_for("fr") == {Tool.OCR, Tool.NER}


def test_installed_languages_includes_english_always():
    """English is bundled, not downloaded — should always show up."""
    assert "en" in installed_languages()


def test_installed_languages_includes_french_after_install(monkeypatch, httpserver, tmp_path):
    _patch_french_to_local_server(monkeypatch, httpserver, b"x")
    assert "fr" not in installed_languages()
    download_artifact("fr", Tool.OCR)
    assert "fr" in installed_languages()
