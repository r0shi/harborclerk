"""Generate the Docker Compose service table from docker-compose.yml.

The compose file is the source of truth for the Docker deployment topology.
Hand-transcribing it is how #537 hid: the architecture diagram faithfully
mirrored a compose file that was missing a worker, so the diagram was
"accurate" about a broken system.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def load_services() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())["services"]


def _image(service: dict) -> str:
    if image := service.get("image"):
        return f"`{image}`"
    build = service.get("build") or {}
    dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
    return f"built from `{dockerfile}`" if dockerfile else "built locally"


def _role(service: dict) -> str:
    command = service.get("command") or []
    if isinstance(command, list) and "--queues" in command:
        queues = []
        for token in command[command.index("--queues") + 1 :]:
            if str(token).startswith("-"):
                break
            queues.append(str(token))
        return f"worker — queues: {', '.join(f'`{q}`' for q in queues)}"
    if isinstance(command, list) and command:
        return f"`{command[0]}`"
    return "—"


def generate() -> str:
    services = load_services()
    lines = [
        f"**{len(services)} services.** Generated from `docker-compose.yml`.",
        "",
        "| Service | Image | Role |",
        "|---|---|---|",
    ]
    for name in sorted(services):
        service = services[name] or {}
        lines.append(f"| `{name}` | {_image(service)} | {_role(service)} |")
    return "\n".join(lines)
