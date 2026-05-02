"""Tests for the worker's TMPDIR symlink-resolution shim."""

import os
import tempfile

from harbor_clerk.worker.entry import _resolve_tmpdir_symlinks


def test_resolve_tmpdir_overrides_when_path_is_symlinked(monkeypatch, tmp_path):
    """Simulates the macOS /tmp -> /private/tmp situation.

    The worker's pytesseract calls write temp PNGs to TMPDIR. When the bundled
    tesseract is invoked with a /tmp-prefixed path on macOS, Leptonica can't
    open the file (symlink quirk) and falls back to interpreting the PNG magic
    bytes as a filename, producing binary stderr that crashes pytesseract's
    UTF-8 decode. Resolving TMPDIR to its realpath sidesteps the symlink.
    """
    real = tmp_path / "real_tmp"
    real.mkdir()
    link = tmp_path / "link_tmp"
    link.symlink_to(real)

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(link))
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    monkeypatch.delenv("TMPDIR", raising=False)

    _resolve_tmpdir_symlinks()

    assert os.environ.get("TMPDIR") == str(real) + os.sep
    assert tempfile.tempdir == str(real)


def test_resolve_tmpdir_noop_when_path_is_not_symlinked(monkeypatch, tmp_path):
    """On Linux (or a clean macOS user session) the temp dir is not a symlink.
    Don't touch TMPDIR or tempfile.tempdir in that case."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", "should-not-change")

    _resolve_tmpdir_symlinks()

    assert os.environ.get("TMPDIR") == "should-not-change"


def test_resolve_tmpdir_pytesseract_writes_to_resolved_path(monkeypatch, tmp_path):
    """End-to-end: after running the resolver, pytesseract.NamedTemporaryFile
    (which uses tempfile.gettempdir()) writes to the resolved path. Verifies
    the fix actually changes pytesseract's behavior, not just env state."""
    real = tmp_path / "real_tmp_e2e"
    real.mkdir()
    link = tmp_path / "link_tmp_e2e"
    link.symlink_to(real)

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(link))
    monkeypatch.setattr(tempfile, "tempdir", str(link))

    _resolve_tmpdir_symlinks()

    # tempfile module should now produce paths under the resolved location
    with tempfile.NamedTemporaryFile(prefix="tess_", delete=True) as f:
        assert os.path.realpath(f.name).startswith(str(real))
