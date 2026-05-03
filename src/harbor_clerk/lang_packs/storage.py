"""Disk layout for downloaded language artifacts.

Layout::

    <lang_packs_dir>/<lang_code>/<install_subpath>

Configurable via the ``LANG_PACKS_DIR`` env var. Defaults to a platform-
appropriate Application Support directory on macOS, ``~/.local/share/...``
on Linux. Same convention as the LLM ``models_dir``.

Tesseract is wired to look at this directory by setting the
``TESSDATA_PREFIX`` env var to include the per-language tesseract subdirs
(future work — phase 4 of the language-packs implementation plan).
spaCy models load by extracting their wheel into the per-language ner
subdir and pointing ``spacy.util.load_model_from_path`` at the result.
"""

import os
import platform
from pathlib import Path

from harbor_clerk.languages import LANGUAGES, Tool


def lang_packs_dir() -> Path:
    """Root directory for downloaded language artifacts.

    Override with the ``LANG_PACKS_DIR`` env var (used by tests to point
    at a temp directory; production users should rely on the default).
    """
    env_dir = os.environ.get("LANG_PACKS_DIR")
    if env_dir:
        return Path(env_dir)
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "lang-packs"
    return Path.home() / ".local" / "share" / "harbor-clerk" / "lang-packs"


def lang_dir(lang_code: str) -> Path:
    """Per-language root, where all of that language's tool artifacts live."""
    return lang_packs_dir() / lang_code


def artifact_path(lang_code: str, tool: Tool) -> Path:
    """Where this artifact lives on disk after download.

    Raises KeyError if the language doesn't exist or the language doesn't
    have an artifact for the requested tool — caller is responsible for
    feature-detecting before calling.
    """
    spec = LANGUAGES[lang_code]
    artifact = spec.artifacts[tool]
    return lang_dir(lang_code) / artifact.install_subpath


def tesseract_data_dir(lang_code: str) -> Path:
    """The directory Tesseract should add to TESSDATA_PREFIX for this lang.

    Tesseract reads .traineddata files from each path in TESSDATA_PREFIX
    (colon-separated). We give it one entry per enabled language so it
    finds e.g. ``fra.traineddata`` under ``<lang_packs>/fr/tesseract/``.
    """
    return lang_dir(lang_code) / "tesseract"
