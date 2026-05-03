"""Static map of (language, tool) -> downloadable artifact spec.

Single source of truth for what we know how to install per-language. The
download manager (lang_packs/manager.py) consults this map; the System
Settings UI renders it; the worker reads the user preferences and picks
the right artifacts at job time.

Adding a new language: pin the upstream URL + SHA256 + approximate size
and the on-disk install subpath. SHAs are real values from the upstream
artifacts; ``download_artifact`` will refuse to install anything whose
content hash doesn't match. To refresh a SHA after an upstream republish,
fetch and verify the new artifact, then update both the SHA and the
install subpath (e.g. bump the version segment in the wheel filename).

English ships in the bundle and has no per-language download. Embedder
is intentionally absent — multilingual-e5-small handles 100+ languages
out of the box, so there's no per-language artifact for it.

See docs/superpowers/specs/2026-05-02-language-packs-on-demand-design.md
for the full design and rationale.
"""

from dataclasses import dataclass, field
from enum import Enum


class Tool(Enum):
    """Tools that have per-language artifacts. Extension point — add new
    tools here as Harbor Clerk grows (e.g. a per-language LLM stylist).
    """

    OCR = "ocr"  # Tesseract traineddata
    NER = "ner"  # spaCy *_core_news_sm or *_core_web_sm


@dataclass(frozen=True)
class ArtifactSpec:
    """One downloadable artifact for one (language, tool) pair."""

    url: str  # https URL to fetch from
    sha256: str  # hex digest, exactly 64 chars
    size_bytes: int  # approximate, for UI download-size display
    install_subpath: str  # relative to lang_packs/<lang>/


@dataclass(frozen=True)
class LanguageSpec:
    """Everything we know about a single language."""

    code: str  # ISO 639-1, e.g. "fr"
    display_name: str  # e.g. "French"
    artifacts: dict[Tool, ArtifactSpec] = field(default_factory=dict)


# Curated default list. English is bundled; French is the headline use
# case (currently silently OCR'd as English — see PR #258 release notes).
# Additional languages will land in a follow-up PR once the foundation
# is shipped — adding entries here is purely additive.
LANGUAGES: dict[str, LanguageSpec] = {
    "en": LanguageSpec(
        code="en",
        display_name="English",
        # Bundled in the app — no per-language download. The empty
        # artifacts map is the signal "this language is built-in."
        artifacts={},
    ),
    "fr": LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/fra.traineddata",
                sha256="ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca",
                size_bytes=1_130_365,  # ~1.1 MB
                install_subpath="tesseract/fra.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/fr_core_news_sm-3.8.0/fr_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="7d6ad14cd5078e53147bfbf70fb9d433c6a3865b695fda2657140bbc59a27e29",
                size_bytes=16_271_721,  # ~16 MB
                install_subpath="ner/fr_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
}
