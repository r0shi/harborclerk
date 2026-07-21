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


def _rest_summary() -> str:
    from scripts.gen_docs.generators.rest import generate_summary

    return generate_summary()


def _rest_endpoints() -> str:
    from scripts.gen_docs.generators.rest import generate_full

    return generate_full()


def _db_tables() -> str:
    from scripts.gen_docs.generators.db_tables import generate

    return generate()


def _pipeline_stages() -> str:
    from scripts.gen_docs.generators.pipeline import generate

    return generate()


def _compose_services() -> str:
    from scripts.gen_docs.generators.compose import generate

    return generate()


def _cli_commands() -> str:
    from scripts.gen_docs.generators.cli_commands import generate

    return generate()


GENERATORS: dict[str, Callable[[], str]] = {
    "mcp-tools": _mcp_tools,
    "rest-summary": _rest_summary,
    "rest-endpoints": _rest_endpoints,
    "db-tables": _db_tables,
    "pipeline-stages": _pipeline_stages,
    "compose-services": _compose_services,
    "cli-commands": _cli_commands,
}
