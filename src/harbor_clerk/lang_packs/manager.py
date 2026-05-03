"""Download / verify / remove language pack artifacts.

Synchronous for the initial cut. The REST endpoints wrap calls in
``run_in_executor`` to keep the event loop responsive. A future
enhancement could stream progress over SSE for the Languages UI;
that's not needed yet because per-language artifacts are small (1-50 MB
each, tens of seconds at most on residential broadband).

Each operation is idempotent and safe to retry. Partial downloads are
written to a ``.tmp`` sibling file and atomic-renamed only after the
SHA256 verifies; an interrupted download leaves a ``.tmp`` file but
not a corrupt artifact.
"""

import hashlib
import logging
import shutil
from dataclasses import dataclass
from typing import Literal

import httpx

from harbor_clerk.lang_packs.storage import artifact_path, lang_dir
from harbor_clerk.languages import LANGUAGES, Tool

logger = logging.getLogger(__name__)

DownloadStatus = Literal["installed", "already_installed", "failed"]


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a download_artifact() call."""

    status: DownloadStatus
    error: str | None = None
    bytes_downloaded: int = 0


def download_artifact(lang_code: str, tool: Tool) -> DownloadResult:
    """Fetch + SHA-verify + install one (lang, tool) artifact.

    Idempotent: if the artifact is already on disk and verifies, returns
    ``already_installed`` without re-fetching.
    """
    if lang_code not in LANGUAGES:
        return DownloadResult(status="failed", error=f"unknown language: {lang_code!r}")
    spec = LANGUAGES[lang_code].artifacts.get(tool)
    if spec is None:
        return DownloadResult(
            status="failed",
            error=f"language {lang_code!r} has no {tool.value} artifact",
        )

    target = artifact_path(lang_code, tool)

    if target.exists() and verify_artifact(lang_code, tool):
        return DownloadResult(status="already_installed")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")

    try:
        with httpx.stream("GET", spec.url, timeout=300.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            sha = hashlib.sha256()
            bytes_written = 0
            with open(tmp_target, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    sha.update(chunk)
                    bytes_written += len(chunk)

        actual_sha = sha.hexdigest()
        if actual_sha != spec.sha256:
            tmp_target.unlink(missing_ok=True)
            return DownloadResult(
                status="failed",
                error=f"sha256 mismatch for {lang_code}/{tool.value}: "
                f"expected {spec.sha256[:8]}..., got {actual_sha[:8]}...",
            )

        tmp_target.replace(target)
        logger.info(
            "Installed lang pack %s/%s (%d bytes) at %s",
            lang_code,
            tool.value,
            bytes_written,
            target,
        )
        return DownloadResult(status="installed", bytes_downloaded=bytes_written)
    except Exception as e:
        tmp_target.unlink(missing_ok=True)
        return DownloadResult(status="failed", error=f"{type(e).__name__}: {e}")


def verify_artifact(lang_code: str, tool: Tool) -> bool:
    """Re-check the SHA256 of an on-disk artifact.

    Used by the REST endpoint to detect tampering or bit-rot, and by
    download_artifact() to short-circuit when the artifact already
    matches.
    """
    if lang_code not in LANGUAGES:
        return False
    spec = LANGUAGES[lang_code].artifacts.get(tool)
    if spec is None:
        return False
    target = artifact_path(lang_code, tool)
    if not target.exists():
        return False
    sha = hashlib.sha256()
    with open(target, "rb") as f:
        while chunk := f.read(64 * 1024):
            sha.update(chunk)
    return sha.hexdigest() == spec.sha256


def remove_artifact(lang_code: str, tool: Tool) -> None:
    """Delete a single artifact from disk. Idempotent.

    Cleans up empty parent directories up to (but not including) the
    per-language root, so a fully-removed language leaves only an empty
    ``<lang_packs>/<lang>/`` directory rather than orphaned subdirs.
    """
    if lang_code not in LANGUAGES:
        return
    spec = LANGUAGES[lang_code].artifacts.get(tool)
    if spec is None:
        return
    target = artifact_path(lang_code, tool)
    target.unlink(missing_ok=True)

    # Walk up removing empty intermediate dirs, stopping at the
    # per-language root (we leave that in place even when empty so the
    # UI's "is this language installed at all" check is consistent).
    lang_root = lang_dir(lang_code)
    parent = target.parent
    while parent != lang_root and parent != parent.parent:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break  # not empty, stop


def remove_language(lang_code: str) -> None:
    """Remove every installed artifact for one language. Idempotent.

    Convenience for the "Disable French entirely" UI affordance — saves
    callers from looping over Tool variants themselves and gracefully
    handles partial installs.
    """
    if lang_code not in LANGUAGES:
        return
    root = lang_dir(lang_code)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def installed_tools_for(lang_code: str) -> set[Tool]:
    """Which of this language's artifacts are installed and verified."""
    if lang_code not in LANGUAGES:
        return set()
    spec = LANGUAGES[lang_code]
    return {tool for tool in spec.artifacts if verify_artifact(lang_code, tool)}


def installed_languages() -> list[str]:
    """ISO codes of languages with at least one installed-and-verified
    artifact. English is always included (it's bundled, not installed)."""
    out = []
    for code, spec in LANGUAGES.items():
        if not spec.artifacts:
            # Bundled language — always considered "installed"
            out.append(code)
        elif installed_tools_for(code):
            out.append(code)
    return out
