"""Schema validation for the static (language, tool) -> ArtifactSpec map."""

import pytest

from harbor_clerk.languages import LANGUAGES, Tool


def test_english_is_present_with_no_artifacts():
    """English ships in the bundle. Empty artifacts map is the
    'this language is built-in' signal that the rest of the code uses
    to skip download flows."""
    assert "en" in LANGUAGES
    assert LANGUAGES["en"].artifacts == {}


def test_every_language_spec_has_iso_code_matching_key():
    """Belt-and-suspenders: the dict key and the spec.code field can
    drift apart silently if someone copy-pastes an entry. Catch that."""
    for code, spec in LANGUAGES.items():
        assert spec.code == code, f"key {code!r} != spec.code {spec.code!r}"


def test_every_language_spec_has_non_empty_display_name():
    for code, spec in LANGUAGES.items():
        assert spec.display_name, f"{code} has empty display_name"


def test_every_artifact_has_https_url_and_64_char_sha256():
    """Real SHAs only — no placeholders. The download manager refuses
    to install anything whose content hash doesn't match, so a placeholder
    SHA would just produce confusing 'sha256 mismatch' errors at install
    time. Catch the mistake at test time instead."""
    for code, spec in LANGUAGES.items():
        for tool, artifact in spec.artifacts.items():
            assert artifact.url.startswith("https://"), f"{code}/{tool.value} url not https"
            assert len(artifact.sha256) == 64, (
                f"{code}/{tool.value} sha256 wrong length: {len(artifact.sha256)} (expected 64)"
            )
            assert all(c in "0123456789abcdef" for c in artifact.sha256), (
                f"{code}/{tool.value} sha256 contains non-hex characters"
            )
            assert artifact.size_bytes > 0, f"{code}/{tool.value} size_bytes must be positive"
            assert artifact.install_subpath, f"{code}/{tool.value} install_subpath empty"


def test_french_has_both_ocr_and_ner_artifacts():
    """French is the headline use case (silently OCR'd as English today).
    Both Tesseract and spaCy artifacts must be present."""
    fr = LANGUAGES["fr"]
    assert Tool.OCR in fr.artifacts
    assert Tool.NER in fr.artifacts
    # Sanity-check the install paths reflect the right tool naming
    assert "fra.traineddata" in fr.artifacts[Tool.OCR].install_subpath
    assert "fr_core_news_sm" in fr.artifacts[Tool.NER].install_subpath


def test_install_subpaths_are_relative_not_absolute():
    """install_subpath joins under lang_packs_dir/<lang>/. An absolute
    path would escape that directory at storage.artifact_path() time."""
    for code, spec in LANGUAGES.items():
        for tool, artifact in spec.artifacts.items():
            assert not artifact.install_subpath.startswith("/"), (
                f"{code}/{tool.value} install_subpath is absolute: {artifact.install_subpath}"
            )
            assert ".." not in artifact.install_subpath.split("/"), (
                f"{code}/{tool.value} install_subpath contains parent traversal"
            )


@pytest.mark.parametrize("tool", list(Tool))
def test_tool_enum_has_string_value(tool):
    """The REST API uses tool.value as the URL segment ('/api/languages/{code}/install/{tool}')."""
    assert isinstance(tool.value, str)
    assert tool.value.islower()
