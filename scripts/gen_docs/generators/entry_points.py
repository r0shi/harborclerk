"""Generate the console-script table from pyproject.toml.

`[project.scripts]` is the source of truth for what binaries this package
installs. The hand-written copy in CLAUDE.md listed five entry points with
prose descriptions that had no mechanism keeping them honest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"

# What each entry point is for. Console scripts carry no description of their
# own, so this is the one hand-written element — and
# test_every_entry_point_is_described asserts it covers [project.scripts]
# exactly, so a new script cannot ship undocumented.
DESCRIPTIONS: dict[str, str] = {
    "harbor-clerk-api": "FastAPI server — REST API, MCP endpoint, and the built SPA.",
    "harbor-clerk-worker": "Ingestion worker. Subscribes to one or more queues via `--queues`.",
    "harbor-clerk-watcher": "Watched-folder and IMAP observer; enqueues ingest jobs. One per deployment.",
    "harbor-clerk-seed": "Database seeder.",
    "harbor-clerk": "CLI for agent harnesses. Off by default, gated by `ENABLE_CLI_ACCESS`.",
}


def load_scripts() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return dict(data["project"].get("scripts", {}))


def generate() -> str:
    scripts = load_scripts()
    lines = [
        f"**{len(scripts)} console scripts**, declared in `[project.scripts]`.",
        "",
        "| Command | Target | Purpose |",
        "|---|---|---|",
    ]
    for name in sorted(scripts):
        lines.append(f"| `{name}` | `{scripts[name]}` | {DESCRIPTIONS.get(name, '—')} |")
    return "\n".join(lines)
