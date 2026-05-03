"""Disk layout helpers (lang_packs_dir, lang_dir, artifact_path)."""

from pathlib import Path

import pytest

from harbor_clerk.lang_packs.storage import (
    artifact_path,
    lang_dir,
    lang_packs_dir,
    tesseract_data_dir,
)
from harbor_clerk.languages import LANGUAGES, Tool


def test_lang_packs_dir_uses_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    assert lang_packs_dir() == tmp_path


def test_lang_packs_dir_falls_back_to_platform_default(monkeypatch):
    """When the env var is unset we use a platform-appropriate path.
    Don't assert exact path (varies between macOS and Linux), just that
    it's an absolute Path under the user's home."""
    monkeypatch.delenv("LANG_PACKS_DIR", raising=False)
    result = lang_packs_dir()
    assert result.is_absolute()
    assert str(result).startswith(str(Path.home()))
    assert "lang-packs" in str(result) or "lang_packs" in str(result)


def test_lang_dir_combines_root_and_lang_code(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    assert lang_dir("fr") == tmp_path / "fr"
    assert lang_dir("de") == tmp_path / "de"


def test_artifact_path_combines_lang_dir_and_install_subpath(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    fr_ocr_path = artifact_path("fr", Tool.OCR)
    expected = tmp_path / "fr" / "tesseract" / "fra.traineddata"
    assert fr_ocr_path == expected


def test_artifact_path_raises_for_unknown_language(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    with pytest.raises(KeyError):
        artifact_path("xx", Tool.OCR)


def test_artifact_path_raises_for_missing_tool(monkeypatch, tmp_path):
    """English has no artifacts. Asking for one is a programming error."""
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    with pytest.raises(KeyError):
        artifact_path("en", Tool.OCR)


def test_tesseract_data_dir_is_per_language(monkeypatch, tmp_path):
    """Tesseract takes a colon-separated TESSDATA_PREFIX; we give it one
    entry per enabled language. The dir must exist under the per-language
    root so tessdata lookups don't cross-pollinate."""
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    assert tesseract_data_dir("fr") == tmp_path / "fr" / "tesseract"
    assert tesseract_data_dir("de") == tmp_path / "de" / "tesseract"


def test_artifact_path_matches_real_french_specs(monkeypatch, tmp_path):
    """Sanity check that the static map and the storage helpers agree on
    where French artifacts land."""
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    fr = LANGUAGES["fr"]
    for tool, artifact in fr.artifacts.items():
        path = artifact_path("fr", tool)
        assert str(path).startswith(str(tmp_path))
        assert path.name == artifact.install_subpath.rsplit("/", 1)[-1]
