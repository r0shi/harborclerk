"""Registry mapping generated-block names to the callables that produce them.

Generators are imported lazily inside their factory so that a heavy or broken
import surfaces as "generator X failed" rather than taking down the whole run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Files scanned for generated blocks. A block in one of these files with no
# registered generator is an error, and vice versa — see __main__.
TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "architecture.md",
)


def _mcp_tools() -> str:
    from scripts.gen_docs.generators.mcp_tools import generate

    return generate()


GENERATORS: dict[str, Callable[[], str]] = {
    "mcp-tools": _mcp_tools,
}
