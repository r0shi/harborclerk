"""The numba environment the macOS app gives its Python processes (#846).

`ServiceManager.runtimeCacheEnvironment` (macos/HarborClerkServer) sets
`NUMBA_CACHE_LOCATOR_CLASSES=UserProvidedCacheLocator` and points
`NUMBA_CACHE_DIR` at the first writable directory it finds, so numba never
falls back to its in-tree locator: the package's own `__pycache__`, which in the
app is inside the signed bundle. These tests run a real `from umap import UMAP`
in a subprocess under that environment, with the locked numba, pynndescent and
umap:

- with a writable directory, umap imports and every cache file lands there;
- with none, numba refuses to cache and the import fails loudly (the app logs
  it and topic computation fails), and still nothing is written in-tree.

The locator list never includes the in-tree locator, so neither case writes
into the test environment's site-packages.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Mirrors ServiceManager.runtimeCacheEnvironment.
LOCATOR_CLASSES = "UserProvidedCacheLocator"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("umap") is None or importlib.util.find_spec("pynndescent") is None,
    reason="umap and pynndescent are not installed",
)


def _in_tree_cache_files() -> dict[Path, int]:
    """numba cache files in umap's and pynndescent's package directories, with
    their modification times, so a rewrite of an existing one counts too."""
    found: dict[Path, int] = {}
    for name in ("umap", "pynndescent"):
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin is not None
        for path in Path(spec.origin).parent.rglob("*.nb[ic]"):
            found[path] = path.stat().st_mtime_ns
    return found


def _import_umap(tmp_path: Path, **numba_env: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NUMBA_CACHE_LOCATOR_CLASSES": LOCATOR_CLASSES,
        **numba_env,
    }
    return subprocess.run(
        [sys.executable, "-c", "from umap import UMAP; print('imported')"],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )


def test_umap_imports_and_caches_only_into_the_given_directory(tmp_path: Path) -> None:
    cache = tmp_path / "numba"
    cache.mkdir()
    before = _in_tree_cache_files()

    result = _import_umap(tmp_path, NUMBA_CACHE_DIR=str(cache))

    assert result.returncode == 0, result.stderr[-2000:]
    assert "imported" in result.stdout
    assert list(cache.rglob("*.nbi")), "numba cached nothing into NUMBA_CACHE_DIR"
    assert _in_tree_cache_files() == before, "numba wrote into the package's own __pycache__"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_with_no_writable_directory_numba_refuses_rather_than_writing_in_tree(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    before = _in_tree_cache_files()
    try:
        result = _import_umap(tmp_path, NUMBA_CACHE_DIR=str(locked / "numba"))
    finally:
        locked.chmod(0o700)

    assert result.returncode != 0, "umap imported with no writable cache directory: where did numba cache?"
    assert "no locator available" in result.stderr
    assert _in_tree_cache_files() == before, "numba wrote into the package's own __pycache__"


def test_without_a_directory_numba_refuses_too(tmp_path: Path) -> None:
    """What the app hands numba when every candidate failed: no NUMBA_CACHE_DIR."""
    before = _in_tree_cache_files()

    result = _import_umap(tmp_path)

    assert result.returncode != 0
    assert "no locator available" in result.stderr
    assert _in_tree_cache_files() == before
