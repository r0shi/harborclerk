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
    """Describe what a service runs.

    Compose `command:` comes in three shapes and each needs handling:
    a list starting with an executable (`harbor-clerk-worker ...`), a plain
    string (`server /data --console-address ...`), and a list of bare flags
    with no executable at all (llama-server: its `entrypoint:` override, a script
    that waits for the model file and then execs the image's binary, names the
    program). Naively taking token zero renders `-m` as the role, which is
    meaningless.
    """
    command = service.get("command")
    if isinstance(command, str):
        command = command.split()
    if not isinstance(command, list) or not command:
        return "—"

    if "--queues" in command:
        queues = []
        for token in command[command.index("--queues") + 1 :]:
            if str(token).startswith("-"):
                break
            queues.append(str(token))
        return f"worker — queues: {', '.join(f'`{q}`' for q in queues)}"

    first = str(command[0])
    if first.startswith("-"):
        # Bare flags: the program is the entrypoint (the image's, or an override
        # that execs it), and the Image column already names the image. Better to
        # say nothing than to name a flag.
        return "—"
    return f"`{first}`"


def _profiles(service: dict) -> list[str]:
    return [str(p) for p in service.get("profiles") or []]


def generate() -> str:
    """One row per service. A service behind `profiles:` is not in the default stack, so it is not counted there."""
    services = load_services()
    default = [name for name in services if not _profiles(services[name] or {})]
    opt_in = len(services) - len(default)
    count = f"**{len(default)} services**"
    if opt_in:
        count += f" in the default stack, plus {opt_in} behind an opt-in profile (`docker compose --profile <name>`)"
    lines = [
        f"{count}. Generated from `docker-compose.yml`.",
        "",
        "| Service | Image | Role | Profile |",
        "|---|---|---|---|",
    ]
    for name in sorted(services):
        service = services[name] or {}
        profiles = ", ".join(f"`{p}`" for p in _profiles(service)) or "default"
        lines.append(f"| `{name}` | {_image(service)} | {_role(service)} | {profiles} |")
    return "\n".join(lines)
