"""Properties of the volume the tests' temp directories live on."""

import tempfile
import unicodedata
from pathlib import Path

import pytest


def volume_is_case_insensitive(directory: Path | None = None) -> bool:
    """True on a case-insensitive volume (APFS's default on macOS), False on ext4 (CI)."""
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        probe = Path(tmp) / "CaseProbe"
        probe.write_text("x")
        return (Path(tmp) / "caseprobe").exists()


def volume_is_normalization_insensitive(directory: Path | None = None) -> bool:
    """True where an NFC and an NFD spelling name the same file (APFS), False on ext4 (CI)."""
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        (Path(tmp) / unicodedata.normalize("NFD", "café")).write_text("x")
        return (Path(tmp) / unicodedata.normalize("NFC", "café")).exists()


normalization_insensitive_only = pytest.mark.skipif(
    not volume_is_normalization_insensitive(),
    reason="needs a normalization-insensitive volume (APFS); on ext4 the other spelling is another file",
)


# A test that only means something where `Doc` and `doc` are different files (CI's ext4).
case_sensitive_only = pytest.mark.skipif(
    volume_is_case_insensitive(), reason="needs a case-sensitive volume (ext4); APFS's default is not"
)


# A test that only means something where `Doc` and `doc` are the same file.
# On a case-sensitive volume it would pass without exercising anything.
case_insensitive_only = pytest.mark.skipif(
    not volume_is_case_insensitive(), reason="needs a case-insensitive volume (APFS default); CI's ext4 is not"
)
