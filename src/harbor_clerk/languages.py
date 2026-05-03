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


# Curated default list. English is bundled; the rest each get the
# Tesseract `tessdata_fast` traineddata + the spaCy `*_core_news_sm`
# wheel for NER. Adding a new language means: pick a 3.8.0 spaCy model
# that has NER support, fetch both artifacts, compute SHA, paste here.
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
    "de": LanguageSpec(
        code="de",
        display_name="German",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/deu.traineddata",
                sha256="19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d",
                size_bytes=1_525_436,
                install_subpath="tesseract/deu.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="fec69fec52b1780f2d269d5af7582a5e28028738bd3190532459aeb473bfa3e7",
                size_bytes=14_639_490,
                install_subpath="ner/de_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    "es": LanguageSpec(
        code="es",
        display_name="Spanish",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/spa.traineddata",
                sha256="6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464",
                size_bytes=2_294_433,
                install_subpath="tesseract/spa.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/es_core_news_sm-3.8.0/es_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="e451a83d6df79b87e9eed0cb553f03e99e36a3bab18a7b79f0dcfd1fdf875e12",
                size_bytes=12_884_212,
                install_subpath="ner/es_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    "it": LanguageSpec(
        code="it",
        display_name="Italian",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/ita.traineddata",
                sha256="b8f89e1e785118dac4d51ae042c029a64edb5c3ee42ef73027a6d412748d8827",
                size_bytes=2_701_314,
                install_subpath="tesseract/ita.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/it_core_news_sm-3.8.0/it_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="3f617bf9a8ae0418953cf1fbf014e10272684c4229e882a7fd748b637d0100bf",
                size_bytes=13_030_943,
                install_subpath="ner/it_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    "nl": LanguageSpec(
        code="nl",
        display_name="Dutch",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/nld.traineddata",
                sha256="ced0e5e046a84c908a6aa7accbef9a232c4a5d9a8276691b81c6ee64d02963f6",
                size_bytes=6_050_296,
                install_subpath="tesseract/nld.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/nl_core_news_sm-3.8.0/nl_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="a76978477821f213ca76a46c686df1b1d41462905d4868bc53eac086adca8b7e",
                size_bytes=12_825_227,
                install_subpath="ner/nl_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    "pt": LanguageSpec(
        code="pt",
        display_name="Portuguese",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/por.traineddata",
                sha256="c4932b937207a9514b7514d518b931a99938c02a28a5a5a553f8599ed58b7deb",
                size_bytes=1_982_756,
                install_subpath="tesseract/por.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/pt_core_news_sm-3.8.0/pt_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="c304fa04db3af73cd08a250feacf560506e15a2ec2469bd1b09f06847f6b455c",
                size_bytes=12_985_007,
                install_subpath="ner/pt_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
}
