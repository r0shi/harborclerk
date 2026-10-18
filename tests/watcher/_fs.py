"""Properties of the volume the tests' temp directories live on."""

import tempfile
from pathlib import Path

import pytest


def volume_is_case_insensitive(directory: Path | None = None) -> bool:
    """True on a case-insensitive volume (APFS's default on macOS), False on ext4 (CI)."""
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        probe = Path(tmp) / "CaseProbe"
        probe.write_text("x")
        return (Path(tmp) / "caseprobe").exists()


# A test that only means something where `Doc` and `doc` are the same file.
# On a case-sensitive volume it would pass without exercising anything.
case_insensitive_only = pytest.mark.skipif(
    not volume_is_case_insensitive(), reason="needs a case-insensitive volume (APFS default); CI's ext4 is not"
)
