"""Generate the CLI subcommand table from the argparse tree.

The CLI is a thin shell over the same MCP transport, so its subcommand list
should mirror the MCP tool list exactly. `test_cli_mcp_parity` asserts that;
this generator publishes it.
"""

from __future__ import annotations


def load_subcommands() -> dict[str, str]:
    from harbor_clerk.cli.main import build_parser

    parser = build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            return {
                name: (sub.description or "").strip().splitlines()[0] if sub.description else ""
                for name, sub in choices.items()
            }
    return {}


def generate() -> str:
    commands = load_subcommands()
    lines = [
        f"**{len(commands)} subcommands**, mirroring the MCP tool surface. "
        "Enable with `ENABLE_CLI_ACCESS=true` (Docker) or the macOS Preferences toggle.",
        "",
        "| Command | Description |",
        "|---|---|",
    ]
    for name in sorted(commands):
        summary = commands[name].replace("|", "\\|")
        lines.append(f"| `harbor-clerk {name}` | {summary} |")
    return "\n".join(lines)
