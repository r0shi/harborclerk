"""Tests for the unified tessdata dir setup."""

import hashlib
import os

import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.lang_packs.manager import download_artifact
from harbor_clerk.lang_packs.tesseract_setup import (
    setup_unified_tessdata_dir,
    unified_tessdata_dir,
)
from harbor_clerk.languages import LANGUAGES, ArtifactSpec, LanguageSpec, Tool


@pytest.fixture(autouse=True)
def _isolated_lang_packs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path / "lang-packs"))


def test_setup_creates_unified_dir_and_sets_env(monkeypatch, tmp_path):
    """Even with no bundled tessdata + no packs installed, calling setup
    creates the dir and sets TESSDATA_PREFIX. Worker startup is a no-op
    in that case (just an empty unified dir)."""
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    setup_unified_tessdata_dir()
    assert unified_tessdata_dir().is_dir()
    assert os.environ["TESSDATA_PREFIX"] == str(unified_tessdata_dir())


def test_setup_symlinks_bundled_tessdata(monkeypatch, tmp_path):
    """When TESSDATA_PREFIX points at a real bundled dir, every
    .traineddata file in it gets a symlink in the unified dir."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "eng.traineddata").write_bytes(b"english data")
    (bundled / "osd.traineddata").write_bytes(b"osd data")
    (bundled / "README.md").write_text("not a traineddata")
    monkeypatch.setenv("TESSDATA_PREFIX", str(bundled))

    setup_unified_tessdata_dir()
    unified = unified_tessdata_dir()

    assert (unified / "eng.traineddata").is_symlink()
    assert (unified / "osd.traineddata").is_symlink()
    # Symlinks resolve to the bundled originals
    assert (unified / "eng.traineddata").read_bytes() == b"english data"
    # Non-traineddata files are skipped
    assert not (unified / "README.md").exists()
    # TESSDATA_PREFIX is overridden to the unified dir
    assert os.environ["TESSDATA_PREFIX"] == str(unified)


def test_setup_symlinks_installed_language_packs(monkeypatch, tmp_path, httpserver: HTTPServer):
    """After a pack is installed via the manager, setup picks up its
    .traineddata file."""
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    payload = b"FRENCH-TRAINEDDATA"
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
    download_artifact("fr", Tool.OCR)

    setup_unified_tessdata_dir()
    unified = unified_tessdata_dir()

    assert (unified / "fra.traineddata").is_symlink()
    assert (unified / "fra.traineddata").read_bytes() == payload


def test_setup_is_idempotent(monkeypatch, tmp_path):
    """Calling setup twice is a no-op the second time — symlinks
    already exist."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "eng.traineddata").write_bytes(b"english")
    monkeypatch.setenv("TESSDATA_PREFIX", str(bundled))

    setup_unified_tessdata_dir()
    setup_unified_tessdata_dir()  # second call shouldn't raise

    unified = unified_tessdata_dir()
    assert (unified / "eng.traineddata").is_symlink()


def test_setup_handles_tessdata_prefix_already_pointing_at_unified_dir(monkeypatch, tmp_path):
    """Worker restart scenario: TESSDATA_PREFIX is the unified dir from
    the previous run. We mustn't symlink the unified dir into itself
    (that would create a loop or fail). Setup should detect this and
    skip the bundled-symlink step."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "eng.traineddata").write_bytes(b"english")

    # First run: TESSDATA_PREFIX points at bundled
    monkeypatch.setenv("TESSDATA_PREFIX", str(bundled))
    setup_unified_tessdata_dir()
    unified = unified_tessdata_dir()
    assert (unified / "eng.traineddata").exists()

    # Second run: TESSDATA_PREFIX now points at unified (set by previous call)
    # Setup should not loop or fail. The existing symlink survives.
    setup_unified_tessdata_dir()  # no exception
    assert (unified / "eng.traineddata").is_symlink()
