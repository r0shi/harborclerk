"""OCR language resolution: preferences -> Tesseract `-l` arg.

Reads the operator's ``enabled_languages`` preference, intersects it with
languages whose Tesseract artifact is actually installed, and produces
the ``-l eng+fra+...`` argument that pytesseract expects.

English is always included (it's bundled and is the safe fallback);
turning it off in preferences is impossible by API design.
"""

import logging

from sqlalchemy import select

from harbor_clerk.lang_packs.manager import installed_tools_for
from harbor_clerk.languages import LANGUAGES, Tool
from harbor_clerk.models import User
from harbor_clerk.models.enums import UserRole

logger = logging.getLogger(__name__)

# ISO 639-1 -> Tesseract's 3-letter language code (only differences from
# the ISO code; languages where they match can be derived). Add entries
# as new languages land in LANGUAGES.
_ISO_TO_TESSERACT = {
    "en": "eng",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "nl": "nld",
}


def iso_to_tesseract(iso_code: str) -> str:
    """ISO 639-1 -> Tesseract's 3-letter code. Falls back to the ISO code
    if no mapping is registered (most languages happen to use 3-letter
    codes already)."""
    return _ISO_TO_TESSERACT.get(iso_code, iso_code)


def get_enabled_languages_from_preferences(session) -> list[str]:
    """Read the global enabled_languages preference (single-tenant app, so
    we use any admin user's setting). Returns ISO 639-1 codes.

    Returns ``["en"]`` on a fresh install (no admin yet, no preference
    set yet) — English is always implicitly enabled.
    """
    user = session.execute(
        select(User).where(User.role == UserRole.admin).order_by(User.created_at).limit(1)
    ).scalar_one_or_none()
    if user is None:
        return ["en"]
    prefs = user.preferences or {}
    enabled = prefs.get("enabled_languages")
    if isinstance(enabled, list) and enabled:
        codes = [c for c in enabled if isinstance(c, str) and c in LANGUAGES]
        if "en" not in codes:
            codes.insert(0, "en")
        return codes
    return ["en"]


def resolve_ocr_languages(enabled_iso: list[str]) -> list[str]:
    """Filter the enabled list to ISO codes whose Tesseract pack is
    installed (or built-in). English is always included.

    Returns the ISO codes — the caller maps to Tesseract's 3-letter
    codes via ``iso_to_tesseract`` when building the ``-l`` arg.
    """
    out = ["en"]
    seen = {"en"}
    for code in enabled_iso:
        if code in seen:
            continue
        if code == "en":
            continue
        spec = LANGUAGES.get(code)
        if spec is None:
            continue
        if Tool.OCR not in spec.artifacts:
            # Language exists in our map but doesn't ship an OCR pack
            # (rare; future-proofing for a NER-only language entry).
            continue
        if Tool.OCR not in installed_tools_for(code):
            logger.warning(
                "OCR: language %r is enabled but the Tesseract pack isn't installed; "
                "OCR will skip it. Install via /api/languages/%s/install.",
                code,
                code,
            )
            continue
        out.append(code)
        seen.add(code)
    return out


def tesseract_lang_arg(iso_codes: list[str]) -> str:
    """Build the Tesseract ``-l`` argument from ISO codes.

    >>> tesseract_lang_arg(["en", "fr"])
    'eng+fra'
    """
    return "+".join(iso_to_tesseract(c) for c in iso_codes)


def get_ocr_languages_for_doc(session) -> tuple[list[str], str]:
    """One-stop: read preferences, filter to installed packs, return both
    the ISO list (for ``ocr_languages_used``) and the Tesseract arg
    (for pytesseract).

    Returns ``(["en", "fr"], "eng+fra")`` for the typical post-install
    French case.
    """
    enabled = get_enabled_languages_from_preferences(session)
    iso_codes = resolve_ocr_languages(enabled)
    return iso_codes, tesseract_lang_arg(iso_codes)
