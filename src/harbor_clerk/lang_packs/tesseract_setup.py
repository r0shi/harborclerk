"""Wire Tesseract to find both bundled and per-language-pack .traineddata files.

Tesseract's ``--tessdata-dir`` flag and ``TESSDATA_PREFIX`` env var only
accept a single path, not colon-separated. To make both bundled languages
(English, OSD) and per-language packs (French, etc.) findable, we
maintain a unified directory of symlinks at
``<lang_packs_dir>/_tessdata/`` and point Tesseract at it.

Called once at worker startup. Re-running is safe (idempotent).
"""

import logging
import os
from pathlib import Path

from harbor_clerk.lang_packs.manager import installed_languages, installed_tools_for
from harbor_clerk.lang_packs.storage import lang_packs_dir, tesseract_data_dir
from harbor_clerk.languages import Tool

logger = logging.getLogger(__name__)

# Common bundled-tessdata locations to probe when TESSDATA_PREFIX is unset
# or already points at our unified dir (e.g. on a worker restart). First
# match wins.
_BUNDLED_FALLBACK_PATHS = [
    "/usr/share/tesseract-ocr/4.00/tessdata",  # Debian/Ubuntu
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tessdata",
    "/opt/homebrew/share/tessdata",  # macOS Homebrew (Apple Silicon)
    "/usr/local/share/tessdata",  # macOS Homebrew (Intel)
]


def unified_tessdata_dir() -> Path:
    """The single directory Tesseract sees as its tessdata path."""
    return lang_packs_dir() / "_tessdata"


def _find_bundled_tessdata_dir(unified: Path) -> Path | None:
    """Locate the bundled tessdata directory.

    Honors the existing ``TESSDATA_PREFIX`` env var (set by the macOS
    bundle launcher or by Docker compose) unless it already points at
    our unified dir — in which case we probe the platform fallbacks
    instead, since the unified dir is downstream of the bundled one.
    """
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        env_path = Path(env)
        if env_path.is_dir() and env_path.resolve() != unified.resolve():
            return env_path
    for candidate in _BUNDLED_FALLBACK_PATHS:
        if Path(candidate).is_dir():
            return Path(candidate)
    return None


def _symlink_traineddata_files(src_dir: Path, unified: Path) -> int:
    """Symlink every .traineddata in src_dir into unified. Skips files
    already present (preserves whatever's there). Returns symlinks added.
    """
    if not src_dir.is_dir():
        return 0
    if src_dir.resolve() == unified.resolve():
        return 0  # don't symlink a dir into itself
    added = 0
    for src in src_dir.glob("*.traineddata"):
        link = unified / src.name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(src.resolve())
            added += 1
        except OSError:
            logger.exception("Failed to symlink %s -> %s", link, src)
    return added


def setup_unified_tessdata_dir() -> Path:
    """Create the unified tessdata dir, populate it from bundled + installed
    packs, and override TESSDATA_PREFIX to point at it.

    Idempotent. Returns the unified dir path.

    Limitation: per-language packs installed AFTER worker startup are not
    visible until the next worker restart. Future improvement: refresh
    on-demand at OCR time, or have the manager call this function when
    a new pack is installed.
    """
    unified = unified_tessdata_dir()
    unified.mkdir(parents=True, exist_ok=True)

    bundled = _find_bundled_tessdata_dir(unified)
    if bundled is not None:
        added = _symlink_traineddata_files(bundled, unified)
        if added:
            logger.info("tessdata: symlinked %d bundled file(s) from %s", added, bundled)
    else:
        logger.warning(
            "tessdata: could not locate bundled tessdata dir; OCR may fall back to "
            "tesseract's compile-time default. Set TESSDATA_PREFIX to point at it."
        )

    pack_added = 0
    for lang_code in installed_languages():
        if Tool.OCR not in installed_tools_for(lang_code):
            continue
        pack_added += _symlink_traineddata_files(tesseract_data_dir(lang_code), unified)
    if pack_added:
        logger.info("tessdata: symlinked %d language-pack file(s) into %s", pack_added, unified)

    os.environ["TESSDATA_PREFIX"] = str(unified)
    return unified
